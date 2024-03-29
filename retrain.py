# Copyright 2015 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Simple transfer learning with an Inception v3 architecture model.
This example shows how to take a Inception v3 architecture model trained on
ImageNet images, and train a new top layer that can recognize other classes of
images.
The top layer receives as input a 2048-dimensional vector for each image. We
train a softmax layer on top of this representation. Assuming the softmax layer
contains N labels, this corresponds to learning N + 2048*N model parameters
corresponding to the learned biases and weights.
Here's an example, which assumes you have a folder containing class-named
subfolders, each full of images for each label. The example folder flower_photos
should have a structure like this:
~/flower_photos/daisy/photo1.jpg
~/flower_photos/daisy/photo2.jpg
...
~/flower_photos/rose/anotherphoto77.jpg
...
~/flower_photos/sunflower/somepicture.jpg
The subfolder names are important, since they define what label is applied to
each image, but the filenames themselves don't matter. Once your images are
prepared, you can run the training with a command like this:
bazel build third_party/tensorflow/examples/image_retraining:retrain && \
bazel-bin/third_party/tensorflow/examples/image_retraining/retrain \
--image_dir ~/flower_photos
You can replace the image_dir argument with any folder containing subfolders of
images. The label for each image is taken from the name of the subfolder it's
in.
This produces a new model file that can be loaded and run by any TensorFlow
program, for example the label_image sample code.
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from datetime import datetime
import glob
import hashlib
import os.path
import random
import re
import sys
import tarfile

import numpy as np
from six.moves import urllib
import tensorflow as tf

from tensorflow.python.client import graph_util
from tensorflow.python.framework import tensor_shape
from tensorflow.python.platform import gfile


FLAGS = tf.app.flags.FLAGS

# Input and output file flags.
tf.app.flags.DEFINE_string('image_dir', '',
                           """Path to folders of labeled images.""")
tf.app.flags.DEFINE_string('output_graph', '/tmp/output_graph.pb',
                           """Where to save the trained graph.""")
tf.app.flags.DEFINE_string('output_labels', '/tmp/output_labels.txt',
                           """Where to save the trained graph's labels.""")

# Details of the training configuration.
tf.app.flags.DEFINE_integer('how_many_training_steps', 4000,
                            """How many training steps to run before ending.""")
tf.app.flags.DEFINE_float('learning_rate', 0.01,
                          """How large a learning rate to use when training.""")
tf.app.flags.DEFINE_integer(
    'testing_percentage', 10,
    """What percentage of images to use as a test set.""")
tf.app.flags.DEFINE_integer(
    'validation_percentage', 10,
    """What percentage of images to use as a validation set.""")
tf.app.flags.DEFINE_integer('eval_step_interval', 10,
                            """How often to evaluate the training results.""")
tf.app.flags.DEFINE_integer('train_batch_size', 100,
                            """How many images to train on at a time.""")
tf.app.flags.DEFINE_integer('test_batch_size', 500,
                            """How many images to test on at a time. This"""
                            """ test set is only used infrequently to verify"""
                            """ the overall accuracy of the model.""")
tf.app.flags.DEFINE_integer(
    'validation_batch_size', 100,
    """How many images to use in an evaluation batch. This validation set is"""
    """ used much more often than the test set, and is an early indicator of"""
    """ how accurate the model is during training.""")

# File-system cache locations.
tf.app.flags.DEFINE_string('model_dir', '/tmp/imagenet',
                           """Path to classify_image_graph_def.pb, """
                           """imagenet_synset_to_human_label_map.txt, and """
                           """imagenet_2012_challenge_label_map_proto.pbtxt.""")
tf.app.flags.DEFINE_string(
    'bottleneck_dir', '/tmp/bottleneck',
    """Path to cache bottleneck layer values as files.""")
tf.app.flags.DEFINE_string('final_tensor_name', 'final_result',
                           """The name of the output classification layer in"""
                           """ the retrained graph.""")

# Controls the distortions used during training.
tf.app.flags.DEFINE_boolean(
    'flip_left_right', False,
    """Whether to randomly flip half of the training images horizontally.""")
tf.app.flags.DEFINE_integer(
    'random_crop', 0,
    """A percentage determining how much of a margin to randomly crop off the"""
    """ training images.""")
tf.app.flags.DEFINE_integer(
    'random_scale', 0,
    """A percentage determining how much to randomly scale up the size of the"""
    """ training images by.""")
tf.app.flags.DEFINE_integer(
    'random_brightness', 0,
    """A percentage determining how much to randomly multiply the training"""
    """ image input pixels up or down by.""")

# These are all parameters that are tied to the particular model architecture
# we're using for Inception v3. These include things like tensor names and their
# sizes. If you want to adapt this script to work with another model, you will
# need to update these to reflect the values in the network you're using.
# pylint: disable=line-too-long
DATA_URL = 'http://download.tensorflow.org/models/image/imagenet/inception-2015-12-05.tgz'
# pylint: enable=line-too-long
BOTTLENECK_TENSOR_NAME = 'pool_3/_reshape'
BOTTLENECK_TENSOR_SIZE = 2048
MODEL_INPUT_WIDTH = 299
MODEL_INPUT_HEIGHT = 299
MODEL_INPUT_DEPTH = 3
JPEG_DATA_TENSOR_NAME = 'DecodeJpeg/contents'
RESIZED_INPUT_TENSOR_NAME = 'ResizeBilinear'


def ensure_name_has_port(tensor_name):
  """Makes sure that there's a port number at the end of the tensor name.
  Args:
    tensor_name: A string representing the name of a tensor in a graph.
  Returns:
    The input string with a :0 appended if no port was specified.
  """
  if ':' not in tensor_name:
    name_with_port = tensor_name + ':0'
  else:
    name_with_port = tensor_name
  return name_with_port


def create_image_lists(image_dir, testing_percentage, validation_percentage):
  """Builds a list of training images from the file system.
  Analyzes the sub folders in the image directory, splits them into stable
  training, testing, and validation sets, and returns a data structure
  describing the lists of images for each label and their paths.
  Args:
    image_dir: String path to a folder containing subfolders of images.
    testing_percentage: Integer percentage of the images to reserve for tests.
    validation_percentage: Integer percentage of images reserved for validation.
  Returns:
    A dictionary containing an entry for each label subfolder, with images split
    into training, testing, and validation sets within each label.
  """
  if not gfile.Exists(image_dir):
    print("Image directory '" + image_dir + "' not found.")
    return None
  result = {}
  sub_dirs = [x[0] for x in os.walk(image_dir)]
  for sub_dir in sub_dirs:
    extensions = ['jpg', 'jpeg', 'JPG', 'JPEG']
    file_list = []
    dir_name = os.path.basename(sub_dir)
    print("Looking for images in '" + dir_name + "'")
    for extension in extensions:
      file_glob = os.path.join(image_dir, dir_name, '*.' + extension)
      file_list.extend(glob.glob(file_glob))
    if not file_list:
      print('No files found')
      continue
    label_name = re.sub(r'[^a-z0-9]+', ' ', dir_name.lower())
    training_images = []
    testing_images = []
    validation_images = []
    for file_name in file_list:
      base_name = os.path.basename(file_name)
      # We want to ignore anything after '_nohash_' in the file name when
      # deciding which set to put an image in, the data set creator has a way of
      # grouping photos that are close variations of each other. For example
      # this is used in the plant disease data set to group multiple pictures of
      # the same leaf.
      hash_name = re.sub(r'_nohash_.*$', '', file_name)
      # This looks a bit magical, but we need to decide whether this file should
      # go into the training, testing, or validation sets, and we want to keep
      # existing files in the same set even if more files are subsequently
      # added.
      # To do that, we need a stable way of deciding based on just the file name
      # itself, so we do a hash of that and then use that to generate a
      # probability value that we use to assign it.
      percentage_hash = (int(
          hashlib.sha1(hash_name).hexdigest(), 16) % (65536)) * (100 / 65535.0)
      if percentage_hash < validation_percentage:
        validation_images.append(base_name)
      elif percentage_hash < (testing_percentage + validation_percentage):
        testing_images.append(base_name)
      else:
        training_images.append(base_name)
    result[label_name] = {
        'dir': dir_name,
        'training': training_images,
        'testing': testing_images,
        'validation': validation_images,
    }
  return result


def get_image_path(image_lists, label_name, index, image_dir, category):
  """"Returns a path to an image for a label at the given index.
  Args:
    image_lists: Dictionary of training images for each label.
    label_name: Label string we want to get an image for.
    index: Int offset of the image we want. This will be moduloed by the
    available number of images for the label, so it can be arbitrarily large.
    image_dir: Root folder string of the subfolders containing the training
    images.
    category: Name string of set to pull images from - training, testing, or
    validation.
  Returns:
    File system path string to an image that meets the requested parameters.
  """
  if label_name not in image_lists:
    tf.logging.fatal('Label does not exist %s.', label_name)
  label_lists = image_lists[label_name]
  if category not in label_lists:
    tf.logging.fatal('Category does not exist %s.', category)
  category_list = label_lists[category]
  if not category_list:
    tf.logging.fatal('Category has no images - %s.', category)
  mod_index = index % len(category_list)
  base_name = category_list[mod_index]
  sub_dir = label_lists['dir']
  full_path = os.path.join(image_dir, sub_dir, base_name)
  return full_path


def get_bottleneck_path(image_lists, label_name, index, bottleneck_dir,
                        category):
  """"Returns a path to a bottleneck file for a label at the given index.
  Args:
    image_lists: Dictionary of training images for each label.
    label_name: Label string we want to get an image for.
    index: Integer offset of the image we want. This will be moduloed by the
    available number of images for the label, so it can be arbitrarily large.
    bottleneck_dir: Folder string holding cached files of bottleneck values.
    category: Name string of set to pull images from - training, testing, or
    validation.
  Returns:
    File system path string to an image that meets the requested parameters.
  """
  return get_image_path(image_lists, label_name, index, bottleneck_dir,
                        category) + '.txt'


def create_inception_graph():
  """"Creates a graph from saved GraphDef file and returns a Graph object.
  Returns:
    Graph holding the trained Inception network.
  """
  with tf.Session() as sess:
    with gfile.FastGFile(
        os.path.join(FLAGS.model_dir, 'classify_image_graph_def.pb'), 'r') as f:
      graph_def = tf.GraphDef()
      graph_def.ParseFromString(f.read())
      _ = tf.import_graph_def(graph_def, name='')
  return sess.graph


def run_bottleneck_on_image(sess, image_data, image_data_tensor_name):
  """Runs inference on an image to extract the 'bottleneck' summary layer.
  Args:
    sess: Current active TensorFlow Session.
    image_data: Numpy array of image data.
    image_data_tensor_name: Name string of the input data layer in the graph.
  Returns:
    Numpy array of bottleneck values.
  """
  bottleneck_tensor = sess.graph.get_tensor_by_name(ensure_name_has_port(
      BOTTLENECK_TENSOR_NAME))
  bottleneck_values = sess.run(
      bottleneck_tensor,
      {ensure_name_has_port(image_data_tensor_name): image_data})
  bottleneck_values = np.squeeze(bottleneck_values)
  return bottleneck_values


def maybe_download_and_extract():
  """Download and extract model tar file.
  If the pretrained model we're using doesn't already exist, this function
  downloads it from the TensorFlow.org website and unpacks it into a directory.
  """
  dest_directory = FLAGS.model_dir
  if not os.path.exists(dest_directory):
    os.makedirs(dest_directory)
  filename = DATA_URL.split('/')[-1]
  filepath = os.path.join(dest_directory, filename)
  if not os.path.exists(filepath):

    def _progress(count, block_size, total_size):
      sys.stdout.write('\r>> Downloading %s %.1f%%' %
                       (filename,
                        float(count * block_size) / float(total_size) * 100.0))
      sys.stdout.flush()

    filepath, _ = urllib.request.urlretrieve(DATA_URL,
                                             filepath,
                                             reporthook=_progress)
    print()
    statinfo = os.stat(filepath)
    print('Succesfully downloaded', filename, statinfo.st_size, 'bytes.')
  tarfile.open(filepath, 'r:gz').extractall(dest_directory)


def ensure_dir_exists(dir_name):
  """Makes sure the folder exists on disk.
  Args:
    dir_name: Path string to the folder we want to create.
  """
  if not os.path.exists(dir_name):
    os.makedirs(dir_name)


def get_or_create_bottleneck(sess, image_lists, label_name, index, image_dir,
                             category, bottleneck_dir):
  """Retrieves or calculates bottleneck values for an image.
  If a cached version of the bottleneck data exists on-disk, return that,
  otherwise calculate the data and save it to disk for future use.
  Args:
    sess: The current active TensorFlow Session.
    image_lists: Dictionary of training images for each label.
    label_name: Label string we want to get an image for.
    index: Integer offset of the image we want. This will be modulo-ed by the
    available number of images for the label, so it can be arbitrarily large.
    image_dir: Root folder string  of the subfolders containing the training
    images.
    category: Name string of which  set to pull images from - training, testing,
    or validation.
    bottleneck_dir: Folder string holding cached files of bottleneck values.
  Returns:
    Numpy array of values produced by the bottleneck layer for the image.
  """
  label_lists = image_lists[label_name]
  sub_dir = label_lists['dir']
  sub_dir_path = os.path.join(bottleneck_dir, sub_dir)
  ensure_dir_exists(sub_dir_path)
  bottleneck_path = get_bottleneck_path(image_lists, label_name, index,
                                        bottleneck_dir, category)
  if not os.path.exists(bottleneck_path):
    print('Creating bottleneck at ' + bottleneck_path)
    image_path = get_image_path(image_lists, label_name, index, image_dir,
                                category)
    if not gfile.Exists(image_path):
      tf.logging.fatal('File does not exist %s', image_path)
    image_data = gfile.FastGFile(image_path, 'r').read()
    bottleneck_values = run_bottleneck_on_image(sess, image_data,
                                                JPEG_DATA_TENSOR_NAME)
    bottleneck_string = ','.join(str(x) for x in bottleneck_values)
    with open(bottleneck_path, 'w') as bottleneck_file:
      bottleneck_file.write(bottleneck_string)

  with open(bottleneck_path, 'r') as bottleneck_file:
    bottleneck_string = bottleneck_file.read()
  bottleneck_values = [float(x) for x in bottleneck_string.split(',')]
  return bottleneck_values


def cache_bottlenecks(sess, image_lists, image_dir, bottleneck_dir):
  """Ensures all the training, testing, and validation bottlenecks are cached.
  Because we're likely to read the same image multiple times (if there are no
  distortions applied during training) it can speed things up a lot if we
  calculate the bottleneck layer values once for each image during
  preprocessing, and then just read those cached values repeatedly during
  training. Here we go through all the images we've found, calculate those
  values, and save them off.
  Args:
    sess: The current active TensorFlow Session.
    image_lists: Dictionary of training images for each label.
    image_dir: Root folder string of the subfolders containing the training
    images.
    bottleneck_dir: Folder string holding cached files of bottleneck values.
  Returns:
    Nothing.
  """
  how_many_bottlenecks = 0
  ensure_dir_exists(bottleneck_dir)
  for label_name, label_lists in image_lists.iteritems():
    for category in ['training', 'testing', 'validation']:
      category_list = label_lists[category]
      for index, unused_base_name in enumerate(category_list):
        get_or_create_bottleneck(sess, image_lists, label_name, index,
                                 image_dir, category, bottleneck_dir)
        how_many_bottlenecks += 1
        if how_many_bottlenecks % 100 == 0:
          print(str(how_many_bottlenecks) + ' bottleneck files created.')


def get_random_cached_bottlenecks(sess, image_lists, how_many, category,
                                  bottleneck_dir, image_dir):
  """Retrieves bottleneck values for cached images.
  If no distortions are being applied, this function can retrieve the cached
  bottleneck values directly from disk for images. It picks a random set of
  images from the specified category.
  Args:
    sess: Current TensorFlow Session.
    image_lists: Dictionary of training images for each label.
    how_many: The number of bottleneck values to return.
    category: Name string of which set to pull from - training, testing, or
    validation.
    bottleneck_dir: Folder string holding cached files of bottleneck values.
    image_dir: Root folder string of the subfolders containing the training
    images.
  Returns:
    List of bottleneck arrays and their corresponding ground truthes.
  """
  class_count = len(image_lists.keys())
  bottlenecks = []
  ground_truthes = []
  for unused_i in range(how_many):
    label_index = random.randrange(class_count)
    label_name = image_lists.keys()[label_index]
    image_index = random.randrange(65536)
    bottleneck = get_or_create_bottleneck(sess, image_lists, label_name,
                                          image_index, image_dir, category,
                                          bottleneck_dir)
    ground_truth = np.zeros(class_count, dtype=np.float32)
    ground_truth[label_index] = 1.0
    bottlenecks.append(bottleneck)
    ground_truthes.append(ground_truth)
  return bottlenecks, ground_truthes


def get_random_distorted_bottlenecks(
    sess, graph, image_lists, how_many, category, image_dir,
    jpeg_data_tensor_name, distorted_image_name):
  """Retrieves bottleneck values for training images, after distortions.
  If we're training with distortions like crops, scales, or flips, we have to
  recalculate the full model for every image, and so we can't use cached
  bottleneck values. Instead we find random images for the requested category,
  run them through the distortion graph, and then the full graph to get the
  bottleneck results for each.
  Args:
    sess: Current TensorFlow Session.
    graph: Live Graph holding the distortion and full model networks.
    image_lists: Dictionary of training images for each label.
    how_many: The integer number of bottleneck values to return.
    category: Name string of which set of images to fetch - training, testing,
    or validation.
    image_dir: Root folder string of the subfolders containing the training
    images.
    jpeg_data_tensor_name: Name string of the input layer we feed the image data
    to.
    distorted_image_name: The output node string name of the distortion graph.
  Returns:
    List of bottleneck arrays and their corresponding ground truthes.
  """
  class_count = len(image_lists.keys())
  bottlenecks = []
  ground_truthes = []
  for unused_i in range(how_many):
    label_index = random.randrange(class_count)
    label_name = image_lists.keys()[label_index]
    image_index = random.randrange(65536)
    image_path = get_image_path(image_lists, label_name, image_index, image_dir,
                                category)
    if not gfile.Exists(image_path):
      tf.logging.fatal('File does not exist %s', image_path)
    jpeg_data = gfile.FastGFile(image_path, 'r').read()
    input_jpeg_tensor = graph.get_tensor_by_name(ensure_name_has_port(
        jpeg_data_tensor_name))
    distorted_image = graph.get_tensor_by_name(ensure_name_has_port(
        distorted_image_name))
    # Note that we materialize the distorted_image_data as a numpy array before
    # sending running inference on the image. This involves 2 memory copies and
    # might be optimized in other implementations.
    distorted_image_data = sess.run(distorted_image,
                                    {input_jpeg_tensor: jpeg_data})
    bottleneck = run_bottleneck_on_image(sess, distorted_image_data,
                                         RESIZED_INPUT_TENSOR_NAME)
    ground_truth = np.zeros(class_count, dtype=np.float32)
    ground_truth[label_index] = 1.0
    bottlenecks.append(bottleneck)
    ground_truthes.append(ground_truth)
  return bottlenecks, ground_truthes


def should_distort_images(flip_left_right, random_crop, random_scale,
                          random_brightness):
  """Whether any distortions are enabled, from the input flags.
  Args:
    flip_left_right: Boolean whether to randomly mirror images horizontally.
    random_crop: Integer percentage setting the total margin used around the
    crop box.
    random_scale: Integer percentage of how much to vary the scale by.
    random_brightness: Integer range to randomly multiply the pixel values by.
  Returns:
    Boolean value indicating whether any distortions should be applied.
  """
  return (flip_left_right or (random_crop != 0) or (random_scale != 0) or
          (random_brightness != 0))


def add_input_distortions(flip_left_right, random_crop, random_scale,
                          random_brightness, jpeg_data_name,
                          distorted_image_name):
  """Creates the operations to apply the specified distortions.
  During training it can help to improve the results if we run the images
  through simple distortions like crops, scales, and flips. These reflect the
  kind of variations we expect in the real world, and so can help train the
  model to cope with natural data more effectively. Here we take the supplied
  parameters and construct a network of operations to apply them to an image.
  Cropping
  ~~~~~~~~
  Cropping is done by placing a bounding box at a random position in the full
  image. The cropping parameter controls the size of that box relative to the
  input image. If it's zero, then the box is the same size as the input and no
  cropping is performed. If the value is 50%, then the crop box will be half the
  width and height of the input. In a diagram it looks like this:
  <       width         >
  +---------------------+
  |                     |
  |   width - crop%     |
  |    <      >         |
  |    +------+         |
  |    |      |         |
  |    |      |         |
  |    |      |         |
  |    +------+         |
  |                     |
  |                     |
  +---------------------+
  Scaling
  ~~~~~~~
  Scaling is a lot like cropping, except that the bounding box is always
  centered and its size varies randomly within the given range. For example if
  the scale percentage is zero, then the bounding box is the same size as the
  input and no scaling is applied. If it's 50%, then the bounding box will be in
  a random range between half the width and height and full size.
  Args:
    flip_left_right: Boolean whether to randomly mirror images horizontally.
    random_crop: Integer percentage setting the total margin used around the
    crop box.
    random_scale: Integer percentage of how much to vary the scale by.
    random_brightness: Integer range to randomly multiply the pixel values by.
    jpeg_data_name: String specifying the jpeg data layer's name.
    distorted_image_name: Name string of the output node of the distortion
    graph.
  Returns:
    Nothing.
  """

  jpeg_data = tf.placeholder(tf.string, name=jpeg_data_name)
  decoded_image = tf.image.decode_jpeg(jpeg_data)
  decoded_image_as_float = tf.cast(decoded_image, dtype=tf.float32)
  decoded_image_4d = tf.expand_dims(decoded_image_as_float, 0)
  margin_scale = 1.0 + (random_crop / 100.0)
  resize_scale = 1.0 + (random_scale / 100.0)
  margin_scale_value = tf.constant(margin_scale)
  resize_scale_value = tf.random_uniform(tensor_shape.scalar(),
                                         minval=1.0,
                                         maxval=resize_scale)
  scale_value = tf.mul(margin_scale_value, resize_scale_value)
  precrop_width = tf.mul(scale_value, MODEL_INPUT_WIDTH)
  precrop_height = tf.mul(scale_value, MODEL_INPUT_HEIGHT)
  precrop_shape = tf.pack([precrop_height, precrop_width])
  precrop_shape_as_int = tf.cast(precrop_shape, dtype=tf.int32)
  precropped_image = tf.image.resize_bilinear(decoded_image_4d,
                                              precrop_shape_as_int)
  precropped_image_3d = tf.squeeze(precropped_image, squeeze_dims=[0])
  cropped_image = tf.random_crop(precropped_image_3d,
                                 [MODEL_INPUT_HEIGHT, MODEL_INPUT_WIDTH,
                                  MODEL_INPUT_DEPTH])
  if flip_left_right:
    flipped_image = tf.image.random_flip_left_right(cropped_image)
  else:
    flipped_image = cropped_image
  brightness_min = 1.0 - (random_brightness / 100.0)
  brightness_max = 1.0 + (random_brightness / 100.0)
  brightness_value = tf.random_uniform(tensor_shape.scalar(),
                                       minval=brightness_min,
                                       maxval=brightness_max)
  brightened_image = tf.mul(flipped_image, brightness_value)
  tf.expand_dims(brightened_image, 0, name=distorted_image_name)


def add_final_training_ops(graph, class_count, final_tensor_name,
                           ground_truth_tensor_name):
  """Adds a new softmax and fully-connected layer for training.
  We need to retrain the top layer to identify our new classes, so this function
  adds the right operations to the graph, along with some variables to hold the
  weights, and then sets up all the gradients for the backward pass.
  The set up for the softmax and fully-connected layers is based on:
  https://tensorflow.org/versions/master/tutorials/mnist/beginners/index.html
  Args:
    graph: Container for the existing model's Graph.
    class_count: Integer of how many categories of things we're trying to
    recognize.
    final_tensor_name: Name string for the new final node that produces results.
    ground_truth_tensor_name: Name string of the node we feed ground truth data
    into.
  Returns:
    Nothing.
  """
  bottleneck_tensor = graph.get_tensor_by_name(ensure_name_has_port(
      BOTTLENECK_TENSOR_NAME))
  layer_weights = tf.Variable(
      tf.truncated_normal([BOTTLENECK_TENSOR_SIZE, class_count], stddev=0.001),
      name='final_weights')
  layer_biases = tf.Variable(tf.zeros([class_count]), name='final_biases')
  logits = tf.matmul(bottleneck_tensor, layer_weights,
                     name='final_matmul') + layer_biases
  tf.nn.softmax(logits, name=final_tensor_name)
  ground_truth_placeholder = tf.placeholder(tf.float32,
                                            [None, class_count],
                                            name=ground_truth_tensor_name)
  cross_entropy = tf.nn.softmax_cross_entropy_with_logits(
      logits, ground_truth_placeholder)
  cross_entropy_mean = tf.reduce_mean(cross_entropy)
  train_step = tf.train.GradientDescentOptimizer(FLAGS.learning_rate).minimize(
      cross_entropy_mean)
  return train_step, cross_entropy_mean


def add_evaluation_step(graph, final_tensor_name, ground_truth_tensor_name):
  """Inserts the operations we need to evaluate the accuracy of our results.
  Args:
    graph: Container for the existing model's Graph.
    final_tensor_name: Name string for the new final node that produces results.
    ground_truth_tensor_name: Name string for the node we feed ground truth data
    into.
  Returns:
    Nothing.
  """
  result_tensor = graph.get_tensor_by_name(ensure_name_has_port(
      final_tensor_name))
  ground_truth_tensor = graph.get_tensor_by_name(ensure_name_has_port(
      ground_truth_tensor_name))
  correct_prediction = tf.equal(
      tf.argmax(result_tensor, 1), tf.argmax(ground_truth_tensor, 1))
  evaluation_step = tf.reduce_mean(tf.cast(correct_prediction, 'float'))
  return evaluation_step


def main(_):
  # Set up the pre-trained graph.
  maybe_download_and_extract()
  graph = create_inception_graph()

  # Look at the folder structure, and create lists of all the images.
  image_lists = create_image_lists(FLAGS.image_dir, FLAGS.testing_percentage,
                                   FLAGS.validation_percentage)
  class_count = len(image_lists.keys())
  if class_count == 0:
    print('No valid folders of images found at ' + FLAGS.image_dir)
    return -1
  if class_count == 1:
    print('Only one valid folder of images found at ' + FLAGS.image_dir +
          ' - multiple classes are needed for classification.')
    return -1

  # See if the command-line flags mean we're applying any distortions.
  do_distort_images = should_distort_images(
      FLAGS.flip_left_right, FLAGS.random_crop, FLAGS.random_scale,
      FLAGS.random_brightness)
  ground_truth_tensor_name = 'ground_truth'
  distorted_image_name = 'distorted_image'
  distorted_jpeg_data_tensor_name = 'distorted_jpeg_data'
  sess = tf.Session()

  if do_distort_images:
    # We will be applying distortions, so set upthe operations we'll need.
    add_input_distortions(FLAGS.flip_left_right, FLAGS.random_crop,
                          FLAGS.random_scale, FLAGS.random_brightness,
                          distorted_jpeg_data_tensor_name, distorted_image_name)
  else:
    # We'll make sure we've calculated the 'bottleneck' image summaries and
    # cached them on disk.
    cache_bottlenecks(sess, image_lists, FLAGS.image_dir, FLAGS.bottleneck_dir)

  # Add the new layer that we'll be training.
  train_step, cross_entropy = add_final_training_ops(
      graph, len(image_lists.keys()), FLAGS.final_tensor_name,
      ground_truth_tensor_name)

  # Set up all our weights to their initial default values.
  init = tf.initialize_all_variables()
  sess.run(init)

  # Create the operations we need to evaluate the accuracy of our new layer.
  evaluation_step = add_evaluation_step(graph, FLAGS.final_tensor_name,
                                        ground_truth_tensor_name)

  # Get some layers we'll need to access during training.
  bottleneck_tensor = graph.get_tensor_by_name(ensure_name_has_port(
      BOTTLENECK_TENSOR_NAME))
  ground_truth_tensor = graph.get_tensor_by_name(ensure_name_has_port(
      ground_truth_tensor_name))

  # Run the training for as many cycles as requested on the command line.
  for i in range(FLAGS.how_many_training_steps):
    # Get a catch of input bottleneck values, either calculated fresh every time
    # with distortions applied, or from the cache stored on disk.
    if do_distort_images:
      train_bottlenecks, train_ground_truth = get_random_distorted_bottlenecks(
          sess, graph, image_lists, FLAGS.train_batch_size, 'training',
          FLAGS.image_dir, distorted_jpeg_data_tensor_name,
          distorted_image_name)
    else:
      train_bottlenecks, train_ground_truth = get_random_cached_bottlenecks(
          sess, image_lists, FLAGS.train_batch_size, 'training',
          FLAGS.bottleneck_dir, FLAGS.image_dir)
    # Feed the bottlenecks and ground truth into the graph, and run a training
    # step.
    sess.run(train_step,
             feed_dict={bottleneck_tensor: train_bottlenecks,
                        ground_truth_tensor: train_ground_truth})
    # Every so often, print out how well the graph is training.
    is_last_step = (i + 1 == FLAGS.how_many_training_steps)
    if (i % FLAGS.eval_step_interval) == 0 or is_last_step:
      train_accuracy, cross_entropy_value = sess.run(
          [evaluation_step, cross_entropy],
          feed_dict={bottleneck_tensor: train_bottlenecks,
                     ground_truth_tensor: train_ground_truth})
      print('%s: Step %d: Train accuracy = %.1f%%' % (datetime.now(), i,
                                                      train_accuracy * 100))
      print('%s: Step %d: Cross entropy = %f' % (datetime.now(), i,
                                                 cross_entropy_value))
      validation_bottlenecks, validation_ground_truth = (
          get_random_cached_bottlenecks(
              sess, image_lists, FLAGS.validation_batch_size, 'validation',
              FLAGS.bottleneck_dir, FLAGS.image_dir))
      validation_accuracy = sess.run(
          evaluation_step,
          feed_dict={bottleneck_tensor: validation_bottlenecks,
                     ground_truth_tensor: validation_ground_truth})
      print('%s: Step %d: Validation accuracy = %.1f%%' %
            (datetime.now(), i, validation_accuracy * 100))

  # We've completed all our training, so run a final test evaluation on
  # some new images we haven't used before.
  test_bottlenecks, test_ground_truth = get_random_cached_bottlenecks(
      sess, image_lists, FLAGS.test_batch_size, 'testing',
      FLAGS.bottleneck_dir, FLAGS.image_dir)
  test_accuracy = sess.run(
      evaluation_step,
      feed_dict={bottleneck_tensor: test_bottlenecks,
                 ground_truth_tensor: test_ground_truth})
  print('Final test accuracy = %.1f%%' % (test_accuracy * 100))

  # Write out the trained graph and labels with the weights stored as constants.
  output_graph_def = graph_util.convert_variables_to_constants(
      sess, graph.as_graph_def(), [FLAGS.final_tensor_name])
  with gfile.FastGFile(FLAGS.output_graph, 'w') as f:
    f.write(output_graph_def.SerializeToString())
  with gfile.FastGFile(FLAGS.output_labels, 'w') as f:
    f.write('\n'.join(image_lists.keys()) + '\n')


if __name__ == '__main__':
  tf.app.run()
