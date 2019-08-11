
# Imports
import os
os.environ['TF_CPP_MIN_VLOG_LEVEL'] = '3'
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'

import logging
logging.getLogger('tensorflow').disabled = True

import sys
sys.path.append("../../")

import time

import random

import numpy as np 

import matplotlib.pyplot as plt    

import tensorflow as tf

# set basic values
print(tf.__version__)

tf.enable_eager_execution()

# this keeps it from getting a huge bite of GPU RAM at the start (mostly useful for figuring out memory usage)
gpu_options = tf.GPUOptions(allow_growth=True)
sess = tf.Session(config=tf.ConfigProto(gpu_options=gpu_options))
tf.keras.backend.set_session(sess)

import keras # for data load

# Imports for pruning

import tfkerassurgeon

from tfkerassurgeon import surgeon

from tfkerassurgeon.democratic_selector import DemocraticSelector

from tfkerassurgeon.identify_by_apoz import ApozIdentifier
from tfkerassurgeon.identify_by_duplicate import DuplicateActivationIdentifier
from tfkerassurgeon.identify_by_gradient import InverseGradientIdentifier

from tfkerassurgeon.operations import delete_channels

import tensorflow_model_optimization as tfmot

from tensorflow_model_optimization.python.core.sparsity import keras as sparsity


# Set some static values that can be tweaked to experiment
# Constants

file_name_prefix = "All_Pruning_1_"

continue_old_run= False

keras_verbosity = 0

input_shape = (28, 28, 1)

nb_classes = 10

batch_size = 128#4096

epochs = 1000 # note: using callbacks means it should stop well before any of these happen.

num_of_full_passes = 10

cutoff_acc = 0.99

num_of_batches = 10 # This is probably way to low to get a good value

clipnorm_val = 2.0 # sets the parameter on the basic class.

layers = tf.keras.layers

all_callbacks = [
                    tf.keras.callbacks.EarlyStopping(monitor='val_loss', patience=3, restore_best_weights=True),
                    tf.keras.callbacks.ReduceLROnPlateau(monitor='val_loss', patience=2, cooldown=5)
                ]
print("Using ClipNorm of "+str(clipnorm_val))


# Def helpers and iterators

def grab_plain_batch(batch_size, X_train, Y_train):

    # get the files iterator
    size_batch = batch_size
    last_index = len(X_train) - 1
    x_train = X_train
    y_train = Y_train

    # continue indefinitly
    while True:

        # return one batch at a time
        batch_data = [[],[]]
        for i in range(0, size_batch):
            # just grab items randomly from the training data
            random_index = random.randint(0, last_index)
            batch_data[0].append(x_train[random_index])
            batch_data[1].append(y_train[random_index])

        yield (np.array(batch_data[0]), np.array(batch_data[1]))


def compile(model):

    new_adam = tf.keras.optimizers.Adam(clipnorm=clipnorm_val)

    model.compile(optimizer=new_adam,
                    loss='categorical_crossentropy',
                    metrics=['accuracy'])

def get_dataset():
    # Get the MNIST Dataset

    # Load the Dataset, they provided a nice helper that does all the network and downloading for you
    (X_train, Y_train), (X_test, Y_test) = keras.datasets.mnist.load_data()
    # This is an leterantive to the MNIST numbers dataset that is a computationlally harder problem
    #(X_train, Y_train), (X_test, Y_test) = keras.datasets.fashion_mnist.load_data()

    # we need to make sure that the images are normalized and in the right format
    X_train = X_train.astype('float32')
    X_test = X_test.astype('float32')
    X_train /= 255
    X_test /= 255

    # expand the dimensions to get the shape to (samples, height, width, channels) where greyscale has 1 channel
    X_train = np.expand_dims(X_train, axis=-1)
    X_test = np.expand_dims(X_test, axis=-1)

    # one-hot encoding, this way, each digit has a probability output
    Y_train = keras.utils.np_utils.to_categorical(Y_train, nb_classes)
    Y_test = keras.utils.np_utils.to_categorical(Y_test, nb_classes)

    # log some basic details to be sure things loaded
    #print()
    #print('MNIST data loaded: train:',len(X_train),'test:',len(X_test))
    #print('X_train:', X_train.shape)
    #print('Y_train:', Y_train.shape)
    #print('X_test:', X_test.shape)
    #print('Y_test:', Y_test.shape)

    return (X_train, Y_train), (X_test, Y_test)


# Build all the stuff we need
(X_train, Y_train), (X_test, Y_test) = get_dataset()

# Create a datagenerator 
datagen = tf.keras.preprocessing.image.ImageDataGenerator(width_shift_range=4, height_shift_range=4, horizontal_flip=False, vertical_flip=False, fill_mode = 'constant', cval = 0.0)

datagen.fit(X_train)

datagen = datagen.flow(X_train, Y_train, batch_size=batch_size)


train_batcher = grab_plain_batch(batch_size, X_train, Y_train)
test_batcher = grab_plain_batch(batch_size, X_test, Y_test)

identifiers = [#ApozIdentifier(),# performance is too bad on this one. Needs a rewrite
               DuplicateActivationIdentifier(batch_size),# perfomance of this one is'nt very good either
               InverseGradientIdentifier(0.15)]# param here isn't used, needs to be cleaned up

selector = DemocraticSelector(identifiers, [train_batcher, datagen], num_of_batches=10)


# Build the model

model = tf.keras.models.Sequential()

model.add(layers.Conv2D(256, (3, 3), input_shape=(28, 28, 1), activation='relu', name='conv_1'))
model.add(layers.SpatialDropout2D(0.25))
#model.add(layers.BatchNormalization())
model.add(layers.MaxPool2D())
model.add(layers.Conv2D(256, (3, 3), activation='relu', name='conv_2'))
model.add(layers.SpatialDropout2D(0.25))
#model.add(layers.BatchNormalization())
model.add(layers.MaxPool2D())
model.add(layers.Flatten())
model.add(layers.Dense(256, activation='relu', name='dense_1'))
model.add(layers.Dropout(0.25))
#model.add(layers.BatchNormalization())
model.add(layers.Dense(10, activation='softmax', name='dense_2'))



if (continue_old_run):

    # load the previous run from a checkpoint
    model = tf.keras.models.load_model(file_name_prefix+'checkpoint_raw_weights.h5')
    model.summary()
    # Compile the model
    compile(model)

else:

    # We're going to loop through the pruning process multiple times
    # We're Going to prune them one layer at a time. 
    model.summary()
    compile(model)
    # Save the model as the current checkpoints 
    model.save(file_name_prefix+'raw_weights.h5') 
    model.save(file_name_prefix+'pruned_raw_weights.h5') 
    model.save(file_name_prefix+'checkpoint_raw_weights.h5') 


# Lets print of the first image just to be sure everything loaded
#first_batch = next(train_batcher)
#print(np.argmax(first_batch[1][0]))

#plt.imshow(np.squeeze(first_batch[0][0], axis=-1), cmap='gray', interpolation='none')
#plt.show()

# run it through everything num_of_full_passes times for good measure
for full_pass_id in range(1, num_of_full_passes + 1):

  escape_loop = False

  # list off the layers we are pruning and the order that we should prune them
  prune_targets = ['conv_1', 'conv_2', 'dense_1']
  # suffle the list because sometimes it gets one layer pruned too much and that stops the rest
  random.shuffle(prune_targets)
  print("Starting Pass ", full_pass_id)

  # this will run until the exit condition is hit
  while (escape_loop != True):

    # prune each layer one at a time
    for prune_target in prune_targets:

      print("Starting Prune for Layer: ", prune_target)

      # Run the training
      #model.fit(X_train, Y_train, epochs=epochs, batch_size=batch_size,
      #    verbose=keras_verbosity, validation_data=(X_test, Y_test))
      
      start_time = time.time()

      model.fit_generator(datagen, 
                          validation_data = test_batcher,
                          validation_steps = 25,
                          steps_per_epoch=256, 
                          epochs=epochs,
                          verbose=keras_verbosity,
                          max_queue_size=10000, 
                          workers=1, 
                          callbacks = all_callbacks)

      print("Fit took %s seconds" % (time.time() - start_time))

      # Then Print the Training Results
      score = model.evaluate(X_test, Y_test, verbose=keras_verbosity)
      print('Test score:', score[0])
      print('Test accuracy:', score[1])

      # check the score did not fall below the threshold, if so, undo the change # Round the value to 3 decimal places, it's stupid to stop because its 0.0004 below
      if (ceil(score[1], 3) < cutoff_acc):
        print("Score was below the Cutoff. ", score[1], cutoff_acc)

        # Clear everything from memory
        del model
        tf.keras.backend.clear_session()

        # load the model from the last backup
        model = tf.keras.models.load_model(file_name_prefix+'checkpoint_raw_weights.h5')
        model.save(file_name_prefix+'pruned_raw_weights.h5') 
        model.summary()

        # Recompile the model
        compile(model)

        # break out of this loop, to do the next 
        escape_loop = True
        break
      # if the accuracy is good, then prune it

      # Save a backup before we prune
      model.save(file_name_prefix+'checkpoint_pruning.h5') 
      
      # Test the total time to predict the whole Validation set
      start_time = time.time()
      model.predict(X_test, verbose=keras_verbosity)
      print("--- %s seconds ---" % (time.time() - start_time))

      # Print our 'Efficency' as the Accuracy / Total Time
      print("Efficency: ", score[1]/(time.time() - start_time))






      print("Starting pruning process")

      # First we get the layer we are working on
      layer = model.get_layer(name = prune_target)
     
      # set the Prune intensity to slowly increase as we go further 
      prune_intensity = ((float(full_pass_id) / float(num_of_full_passes)) + 0.25) / 2.0
      print("Using pruning intensity: ", prune_intensity)
      
      start_time = time.time()

      selected_to_prune = selector.get_selection(prune_intensity, model, layer)

      print("Selecting layers took %s seconds" % (time.time() - start_time))

      print("Pruning out these layers based on votes:", selected_to_prune)


      # if there are only a few outputs to prune, lets move on to the next one. 
      # as we get deeper into the pruneing loops we lower the bar
      if(len(selected_to_prune) <= 1):#(num_of_full_passes + 1 - full_pass_id) - 5 ):
                
        print("Outputs to prune were less than limit.", len(selected_to_prune))# (num_of_full_passes + 1 - full_pass_id) - 5 )

        # Clear everything from memory
        del model
        tf.keras.backend.clear_session()

        # load the model from the raw weights so we can train again
        model = tf.keras.models.load_model(file_name_prefix+'pruned_raw_weights.h5')
        model.summary()

        # Recompile the model
        compile(model)

        # break out of this loop, to do the next 
        escape_loop = True
        break


      # load the raw weights model and prune from it instead
      model = tf.keras.models.load_model(file_name_prefix+'pruned_raw_weights.h5')      
      # save the previous weights as a checkpoint to go back to if exit condition is hit
      model.save(file_name_prefix+'checkpoint_raw_weights.h5') 
            
      # First we get the layer we are working on
      layer = model.get_layer(name=prune_target)

      try:

          # Run the pruning on the Model and get the Pruned (uncompiled) model as a result
          model = delete_channels(model, layer, selected_to_prune)

      except Exception as ex:

        print("Could not delete layers")
       
        print(ex)

        # Clear everything from memory
        del model
        tf.keras.backend.clear_session()

        # load the model from the raw weights so we can train again
        model = tf.keras.models.load_model(file_name_prefix+'checkpoint_raw_weights.h5')
        model.save(file_name_prefix+'pruned_raw_weights.h5') 
        model.summary()

        # Recompile the model
        compile(model)

        # break out of this loop, to do the next 
        escape_loop = True
        break
        

      # Save a the new raw weights after we prune
      model.save(file_name_prefix+'pruned_raw_weights.h5') 

      # Clear everything from memory
      del model
      tf.keras.backend.clear_session()

      # load the model from the raw weights so we can train again
      model = tf.keras.models.load_model(file_name_prefix+'pruned_raw_weights.h5')

      # Recompile the model
      compile(model)

      print("Loop finished.")


# One final training to make sure it fits well
model.fit_generator(datagen,
        validation_data = test_batcher,
        validation_steps = 25,
        steps_per_epoch=256,
        epochs=epochs,
        verbose=1,
        callbacks = all_callbacks
        )


# Test the total time to predict the whole Validation set
start_time = time.time()
model.predict(X_test, verbose=keras_verbosity)
print("--- %s seconds ---" % (time.time() - start_time))


# Print our 'Efficency' as the Accuracy / Total Time
print("Efficency: ", score[1]/(time.time() - start_time))


model.save(file_name_prefix+'pruned_model.h5')
model.save_weights(file_name_prefix+'pruned_model_weights.h5')


# Clear everything from memory
del model
tf.keras.backend.clear_session()


# Build a very small Dense net as an example

model = tf.keras.models.Sequential()

model.add(layers.Conv2D(20,
                 (3, 3),
                 input_shape=(28, 28, 1),
                 activation='relu',
                 name='conv_1'))
model.add(layers.MaxPool2D())
model.add(layers.Conv2D(50, (3, 3), activation='relu', name='conv_2'))
model.add(layers.MaxPool2D())
model.add(layers.Permute((2, 1, 3)))
model.add(layers.Flatten())
model.add(layers.Dense(500, activation='relu', name='dense_1'))
model.add(layers.Dense(10, activation='softmax', name='dense_2'))

compile(model)

model.summary()





# Run the training

model.fit(
          X_train,
          Y_train,
          epochs=epochs,
          batch_size=batch_size,
          verbose=1,
          validation_data=(X_test, Y_test),
          callbacks = all_callbacks
         )


# Then Print the Training Results
score = model.evaluate(X_test, Y_test, verbose=keras_verbosity)
print('Test score:', score[0])
print('Test accuracy:', score[1])


# Test the total time to predict the whole Validation set
start_time = time.time()
model.predict(X_test, verbose=keras_verbosity)
print("--- %s seconds ---" % (time.time() - start_time))


# Print our 'Efficency' as the Accuracy / Total Time
print(score[1]/(time.time() - start_time))
