import os
import argparse
import logging
from pathlib import Path
from ast import literal_eval
from multiprocessing import cpu_count
import tensorflow as tf
import tensorflow_io as tfio
import smdebug.tensorflow as smd
from smdebug.core.reduction_config import ReductionConfig
from smdebug.core.save_config import SaveConfig
from smdebug.core.collection import CollectionKeys
from smdebug.core.config_constants import DEFAULT_CONFIG_FILE_PATH
from utils.dist_utils import is_sm_dist
from engine.schedulers import WarmupScheduler
from engine.optimizers import MomentumOptimizer
from data.datasets import create_dataset, parse
if is_sm_dist():
    import smdistributed.dataparallel.tensorflow as dist
else:
    import horovod.tensorflow.keras as dist

def parse_args():
    cmdline = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    cmdline.add_argument('--train_data_dir', default='/opt/ml/input/data/train',
                         help="""Path to dataset in TFRecord format
                             (aka Example protobufs). Files should be
                             named 'train-*' and 'validation-*'.""")
    cmdline.add_argument('--validation_data_dir', default='/opt/ml/input/data/validation',
                         help="""Path to dataset in TFRecord format
                             (aka Example protobufs). Files should be
                             named 'train-*' and 'validation-*'.""")
    cmdline.add_argument('--num_classes', default=1000, type=int,
                         help="""Number of classes.""")
    cmdline.add_argument('--train_dataset_size', default=1281167, type=int,
                         help="""Number of images in training data.""")
    cmdline.add_argument('--model_dir', default='/opt/ml/checkpoints',
                         help="""Path to save model with best accuracy""")
    cmdline.add_argument('-b', '--batch_size', default=128, type=int,
                         help="""Size of each minibatch per GPU""")
    cmdline.add_argument('--warmup_steps', default=500, type=int,
                         help="""steps for linear learning rate warmup""")
    cmdline.add_argument('--num_epochs', default=120, type=int,
                         help="""Number of epochs to train for.""")
    cmdline.add_argument('--schedule', default='cosine', type=str,
                         help="""learning rate schedule""")
    cmdline.add_argument('-lr', '--learning_rate', default=0.1, type=float,
                         help="""Start learning rate.""")
    cmdline.add_argument('--momentum', default=0.9, type=float,
                         help="""Start optimizer momentum.""")
    cmdline.add_argument('--label_smoothing', default=0.1, type=float,
                         help="""Label smoothing value.""")
    cmdline.add_argument('--mixup_alpha', default=0.2, type=float,
                        help="""Mixup beta distribution shape parameter. 0.0 disables mixup.""")
    cmdline.add_argument('--l2_weight_decay', default=1e-4, type=float,
                         help="""L2 weight decay multiplier.""")
    cmdline.add_argument('-fp16', '--fp16', default='True',
                         help="""disable mixed precision training""")
    cmdline.add_argument('-xla', '--xla', default='True',
                         help="""enable xla""")
    cmdline.add_argument('-tf32', '--tf32', default='True',
                         help="""enable tensorflow-32""")
    cmdline.add_argument('--model',
                         help="""Which model to train. Options are:
                         resnet50v1_b, resnet50v1_c, resnet50v1_d, 
                         resnet101v1_b, resnet101v1_c,resnet101v1_d, 
                         resnet152v1_b, resnet152v1_c,resnet152v1_d,
                         resnet50v2, resnet101v2, resnet152v2
                         darknet53, hrnet_w18c, hrnet_w32c""")
    cmdline.add_argument('--resume_from', 
                         help='Path to SavedModel format model directory from which to resume training')
    cmdline.add_argument('--pipe_mode', default='False',
                         help='Path to SavedModel format model directory from which to resume training')
    return cmdline

def create_hook():
    if Path(DEFAULT_CONFIG_FILE_PATH).exists():
        hook = smd.KerasHook.create_from_json_file()
    else:
        reduction_config = ReductionConfig(['mean'])
        save_config = SaveConfig(save_interval=25)
        include_collections = [CollectionKeys.LOSSES]

        hook_config = {
            'out_dir' : './smdebug/',
            'export_tensorboard': True,
            'tensorboard_dir': './smdebug/tensorboard/',
            'dry_run': False,
            'reduction_config': reduction_config,
            'save_config': save_config,
            'include_regex': None,
            'include_collections': include_collections,
            'save_all': False,
            'include_workers': 'one',
        }

        hook = smd.KerasHook(**hook_config)
    return hook

def main(FLAGS):
    dist.init()
    gpus = tf.config.experimental.list_physical_devices('GPU')
    for gpu in gpus:
        tf.config.experimental.set_memory_growth(gpu, True)
    if gpus:
        device = gpus[dist.local_rank()]
        tf.config.experimental.set_visible_devices(device, 'GPU')
    else:
        device = None
    # tf.config.threading.intra_op_parallelism_threads = 1 # Avoid pool of Eigen threads
    tf.config.threading.inter_op_parallelism_threads = max(2, cpu_count()//dist.local_size()-2)
    tf.config.optimizer.set_jit(FLAGS.xla)
    tf.config.optimizer.set_experimental_options({"auto_mixed_precision": FLAGS.fp16})
    tf.config.experimental.enable_tensor_float_32_execution(FLAGS.tf32)
    # policy = tf.keras.mixed_precision.Policy('mixed_float16' if FLAGS.fp16 else 'float32')
    # tf.keras.mixed_precision.set_global_policy(policy)
    preprocessing_type = 'resnet'
    if FLAGS.model == 'resnet50':
        model = tf.keras.applications.ResNet50(weights=None, classes=FLAGS.num_classes)
    else:
        raise NotImplementedError('Model {} not implemented'.format(FLAGS.model))

    steps_per_epoch = FLAGS.train_dataset_size // FLAGS.batch_size
    iterations = steps_per_epoch * FLAGS.num_epochs
    batch_size_per_device = FLAGS.batch_size//dist.size()

    # 5 epochs are for warmup
    if FLAGS.schedule == 'piecewise_short':
        scheduler = tf.keras.optimizers.schedules.PiecewiseConstantDecay(
                    boundaries=[iterations//5, 
                                iterations//2, 
                                int(iterations*0.7), 
                                int(iterations*0.9)], 
                    values=[FLAGS.learning_rate, FLAGS.learning_rate * 0.1, 
                            FLAGS.learning_rate * 0.01, FLAGS.learning_rate * 0.001, 
                            FLAGS.learning_rate * 0.0001])
    elif FLAGS.schedule == 'piecewise_long':
        scheduler = tf.keras.optimizers.schedules.PiecewiseConstantDecay(
                    boundaries=[iterations//4, 
                                int(iterations*0.6), 
                                int(iterations*0.9)], 
                    values=[FLAGS.learning_rate, FLAGS.learning_rate * 0.1, FLAGS.learning_rate * 0.01, FLAGS.learning_rate * 0.001])
    elif FLAGS.schedule == 'cosine':
        scheduler = tf.keras.experimental.CosineDecayRestarts(initial_learning_rate=FLAGS.learning_rate,
                    first_decay_steps=iterations, t_mul=1, m_mul=1, alpha=1e-3)
    else:
        print('No schedule specified')


    # scheduler = WarmupScheduler(scheduler=scheduler, initial_learning_rate=FLAGS.learning_rate * .01, warmup_steps=FLAGS.warmup_steps)

    # TODO support optimizers choice via config
    opt = tf.keras.optimizers.SGD(learning_rate=scheduler, momentum=FLAGS.momentum, nesterov=True) # needs momentum correction term
    # opt = MomentumOptimizer(learning_rate=scheduler, momentum=FLAGS.momentum, nesterov=True) 
    
    # if FLAGS.fp16:
    #     # opt = tf.train.experimental.enable_mixed_precision_graph_rewrite(opt, loss_scale=128.)
    #     opt = tf.keras.mixed_precision.LossScaleOptimizer(opt, dynamic=False, 
    #                                                       initial_scale=128., 
    #                                                       dynamic_growth_steps=None)
    
    opt = dist.DistributedOptimizer(opt)
    
    # FLAGS.label_smoothing = tf.cast(FLAGS.label_smoothing, tf.float16 if FLAGS.fp16 else tf.float32)
    loss_func = tf.keras.losses.CategoricalCrossentropy(from_logits=True, 
                                                        label_smoothing=FLAGS.label_smoothing,
                                                        reduction=tf.keras.losses.Reduction.SUM_OVER_BATCH_SIZE) 
    
    model.compile(optimizer=opt, loss=loss_func, metrics=['accuracy'])
    
    callbacks = [dist.callbacks.BroadcastGlobalVariablesCallback(0), create_hook()]
    
    train_data = create_dataset(FLAGS.train_data_dir, batch_size_per_device, 
                                preprocessing=preprocessing_type, pipe_mode=FLAGS.pipe_mode, 
                                device=device)
    validation_data = create_dataset(FLAGS.validation_data_dir, batch_size_per_device, 
                                     preprocessing=preprocessing_type, train=False, pipe_mode=FLAGS.pipe_mode, 
                                     device=device)
    
    model.fit(train_data, epochs=5, validation_data=validation_data, verbose=dist.rank()==0, callbacks=callbacks)

if __name__ == '__main__':
    cmdline = parse_args()
    FLAGS, unknown_args = cmdline.parse_known_args()
    FLAGS.fp16 = literal_eval(FLAGS.fp16)
    FLAGS.xla = literal_eval(FLAGS.xla)
    FLAGS.tf32 = literal_eval(FLAGS.tf32)
    FLAGS.pipe_mode = literal_eval(FLAGS.pipe_mode)
    main(FLAGS)
