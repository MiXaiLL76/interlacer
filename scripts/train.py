"""Script to launch a training job.

  Loads dataset and model from training config information, sets up checkpointing and tensorboard callbacks, and starts training.

  Usage:

    $ python train.py /path/to/config.ini --debug --experiment loss_comparison_runs --suffix trial1

  Options:

    --debug(Boolean): Only train for 5 epochs on limited data, and delete temp logs
    --experiment(string): Optional label for a higher-level directory in which to store this run's log directory
    --suffix(string): Optional, arbitrary tag to append to job name
"""

import argparse
import atexit
import os
import pickle
from shutil import copyfile, rmtree

import numpy as np
import tensorflow as tf
from tensorflow import keras
from tensorflow.keras import backend as K
from tensorflow.keras.datasets import mnist

import filepaths
import training_config
from interlacer import data_generator, layers, losses, models, utils

gpus = tf.config.experimental.list_physical_devices('GPU')
gpu_options = tf.compat.v1.GPUOptions(per_process_gpu_memory_fraction=0.5)
config = tf.compat.v1.ConfigProto(gpu_options=gpu_options)
config.gpu_options.allow_growth = True

if gpus:
  # Restrict TensorFlow to only use the first GPU
    try:
        tf.config.experimental.set_visible_devices(gpus[0], 'GPU')
        logical_gpus = tf.config.experimental.list_logical_devices('GPU')
        print(len(gpus), "Physical GPUs,", len(logical_gpus), "Logical GPU")
    except RuntimeError as e:
        # Visible devices must be set before GPUs have been initialized
        print(e)

# Parse args
parser = argparse.ArgumentParser(
    description='Train a Fourier-domain neural network to correct corrupted k-space data.')
parser.add_argument('config', help='Path to .ini config file.')
parser.add_argument('--experiment', help='Experiment folder name.')
parser.add_argument(
    '--suffix',
    help='Descriptive suffix appended to job name.')
parser.add_argument(
    '--debug',
    help='Boolean indicating whether to run small-scale training experiment.',
    action='store_true')

# Set up config
args = parser.parse_args()
config_path = args.config
experiment = args.experiment
suffix = args.suffix
debug = args.debug

exp_config = training_config.TrainingConfig(config_path)
exp_config.read_config()

# Load dataset
if(exp_config.dataset == 'MRI'):
    img_train, img_val = data_generator.get_mri_images()
elif(exp_config.dataset == 'MNIST'):
    img_train, img_val = data_generator.get_mnist_images()
    
train_generator = data_generator.generate_data(
    img_train,
    exp_config.task,
    exp_config.input_domain,
    exp_config.output_domain,
    exp_config.corruption_frac,
    exp_config.batch_size,
    'train')
print('Generated training generator')

val_generator = data_generator.generate_data(
    img_val,
    exp_config.task,
    exp_config.input_domain,
    exp_config.output_domain,
    exp_config.corruption_frac,
    exp_config.batch_size,
    'val')
print('Generated validation generator')

# Pick architecture
n = img_train.shape[1]
if(exp_config.architecture == 'CONV'):
    model = models.get_conv_no_residual_model(
        (n,
         n,
         2),
        exp_config.nonlinearity,
        exp_config.kernel_size,
        exp_config.num_features,
        exp_config.num_layers)
elif(exp_config.architecture == 'CONV_RESIDUAL'):
    model = models.get_conv_residual_model(
        (n,
         n,
         2),
        exp_config.nonlinearity,
        exp_config.kernel_size,
        exp_config.num_features,
        exp_config.num_layers)
elif(exp_config.architecture == 'INTERLACER_RESIDUAL'):
    model = models.get_interlacer_residual_model(
        (n,
         n,
         2),
        exp_config.nonlinearity,
        exp_config.kernel_size,
        exp_config.num_features,
        exp_config.num_layers)
print('Loaded model')

# Checkpointing
job_name = exp_config.job_name
if(debug):
    job_name = 'debug_job' + str(np.random.randint(0, 10))
if(suffix is not None):
    job_name += '*' + suffix
dir_path = filepaths.TRAIN_DIR
if(experiment is not None and not debug):
    dir_path += experiment + '/'
checkpoint_dir = os.path.join(dir_path, job_name)
checkpoint_name = 'cp-{epoch:04d}.ckpt'
checkpoint_path = os.path.join(checkpoint_dir, checkpoint_name)
if not os.path.exists(checkpoint_dir):
    os.makedirs(checkpoint_dir)
cp_callback = keras.callbacks.ModelCheckpoint(
    checkpoint_path, verbose=1, save_weights_only=True, period=5)
print('Set up checkpointing')

if(debug):
    def del_logs():
        rmtree(checkpoint_dir, ignore_errors=True)
        print('Deleted temp debug logs')
    atexit.register(del_logs)

copyfile(args.config, os.path.join(checkpoint_dir, job_name + '_config.ini'))
summary_file = os.path.join(checkpoint_dir, 'summary.txt')
with open(summary_file, 'w') as fh:
    model.summary(print_fn=lambda x: fh.write(x + '\n'))

# Tensorboard
tb_dir = os.path.join(checkpoint_dir, 'tensorboard/')
if os.path.exists(tb_dir):
    raise ValueError(
        'Tensorboard logs have already been created under this name.')
else:
    os.makedirs(tb_dir)
tb_callback = keras.callbacks.TensorBoard(
    log_dir=tb_dir, histogram_freq=0, write_graph=True, write_images=True)

# Select loss
if(exp_config.loss_type=='image'):
    used_loss = losses.image_loss(exp_config.output_domain, exp_config.loss)
elif(exp_config.loss_type=='freq'):
    used_loss = losses.fourier_loss(exp_config.output_domain, exp_config.loss)
else:
    raise ValueError('Unrecognized loss type.')

# Setup model
fourier_l1 = losses.fourier_loss(exp_config.output_domain, 'L1')
fourier_l2 = losses.fourier_loss(exp_config.output_domain, 'L2')
image_l1 = losses.image_loss(exp_config.output_domain, 'L1')
image_l2 = losses.image_loss(exp_config.output_domain, 'L2')
image_mag_l1 = losses.image_mag_loss(exp_config.output_domain, 'L1')

lr = 1e-3
model.compile(optimizer=tf.keras.optimizers.Adam(lr=lr),
              loss=used_loss,
              metrics=[fourier_l1, fourier_l2, image_l1, image_l2])
print('Compiled model')

if(debug):
    print('Number of parameters: ' + str(model.count_params()))

# Train model
if(debug):
    num_epochs = 5
    steps_per_epoch = 2
    val_steps = 1
else:
    num_epochs = exp_config.num_epochs
    steps_per_epoch = int(img_train.shape[0] / exp_config.batch_size)
    val_steps = 8

model.fit_generator(
    train_generator,
    epochs=num_epochs,
    steps_per_epoch=steps_per_epoch,
    validation_data=val_generator,
    validation_steps=val_steps,
    callbacks=[
        cp_callback,
        tb_callback],
    workers=1)
