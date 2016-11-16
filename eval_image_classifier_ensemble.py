# Copyright 2016 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Generic evaluation script that evaluates a model using a given dataset."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import math
import numpy as np
import tensorflow as tf

from datasets import dataset_factory
from nets import nets_factory
from preprocessing import preprocessing_factory

slim = tf.contrib.slim

tf.app.flags.DEFINE_integer(
    'batch_size', 100, 'The number of samples in each batch.')

tf.app.flags.DEFINE_integer(
    'max_num_batches', None,
    'Max number of batches to evaluate by default use all.')

tf.app.flags.DEFINE_string(
    'master', '', 'The address of the TensorFlow master to use.')

tf.app.flags.DEFINE_string(
    'checkpoint_path', '/tmp/tfmodel/',
    'The directory where the model was written to or an absolute path to a '
    'checkpoint file.')

tf.app.flags.DEFINE_string(
    'eval_dir', '/tmp/tfmodel/', 'Directory where the results are saved to.')

tf.app.flags.DEFINE_integer(
    'num_preprocessing_threads', 1,
    'The number of threads used to create the batches.')

tf.app.flags.DEFINE_string(
    'dataset_name', 'imagenet', 'The name of the dataset to load.')

tf.app.flags.DEFINE_string(
    'dataset_split_name', 'test', 'The name of the train/test split.')

tf.app.flags.DEFINE_string(
    'dataset_dir', None, 'The directory where the dataset files are stored.')

tf.app.flags.DEFINE_integer(
    'labels_offset', 0,
    'An offset for the labels in the dataset. This flag is primarily used to '
    'evaluate the VGG and ResNet architectures which do not use a background '
    'class for the ImageNet dataset.')

tf.app.flags.DEFINE_string(
    'model_name', 'inception_v3', 'The name of the architecture to evaluate.')

tf.app.flags.DEFINE_string(
    'preprocessing_name', None, 'The name of the preprocessing to use. If left '
    'as `None`, then the model_name flag is used.')

tf.app.flags.DEFINE_float(
    'moving_average_decay', None,
    'The decay to use for the moving average.'
    'If left as None, then moving averages are not used.')

tf.app.flags.DEFINE_integer(
    'eval_image_size', None, 'Eval image size')

FLAGS = tf.app.flags.FLAGS


def main(_):
    if not FLAGS.dataset_dir:
        raise ValueError('You must supply the dataset directory with --dataset_dir')

    checkpoint_paths = []
    for checkpoint_path in FLAGS.checkpoint_path.split(','):
        if tf.gfile.IsDirectory(checkpoint_path):
            path = tf.train.latest_checkpoint(checkpoint_path)
        else:
            path = checkpoint_path
        checkpoint_paths.append(path)

    tf.logging.info('Evaluating %s' % checkpoint_paths)
    output_list = []
    labels_list = []

    for index in range(len(checkpoint_paths)):
        tf.logging.set_verbosity(tf.logging.INFO)
        with tf.Graph().as_default():
            ######################
            # Select the dataset #
            ######################
            dataset = dataset_factory.get_dataset(
                FLAGS.dataset_name, FLAGS.dataset_split_name, FLAGS.dataset_dir)

            ####################
            # Select the model #
            ####################
            network_fn = nets_factory.get_network_fn(
                FLAGS.model_name,
                num_classes=(dataset.num_classes - FLAGS.labels_offset),
                is_training=False)

            ##############################################################
            # Create a dataset provider that loads data from the dataset #
            ##############################################################
            provider = slim.dataset_data_provider.DatasetDataProvider(
                dataset,
                shuffle=False,
                common_queue_capacity=2 * FLAGS.batch_size,
                common_queue_min=FLAGS.batch_size)
            [image, label] = provider.get(['image', 'label'])
            label -= FLAGS.labels_offset

            #####################################
            # Select the preprocessing function #
            #####################################
            preprocessing_name = FLAGS.preprocessing_name or FLAGS.model_name
            image_preprocessing_fn = preprocessing_factory.get_preprocessing(
                preprocessing_name,
                is_training=False)

            eval_image_size = FLAGS.eval_image_size or network_fn.default_image_size

            image = image_preprocessing_fn(image, eval_image_size, eval_image_size)

            images, labels = tf.train.batch(
                [image, label],
                batch_size=FLAGS.batch_size,
                num_threads=FLAGS.num_preprocessing_threads,
                capacity=5 * FLAGS.batch_size)

            ####################
            # Define the model #
            ####################
            logits, _ = network_fn(images)

            if FLAGS.max_num_batches:
                num_batches = FLAGS.max_num_batches
            else:
                # This ensures that we make a single pass over all of the data.
                num_batches = int(math.ceil(dataset.num_samples / float(FLAGS.batch_size)))

            total_output = np.empty([num_batches * FLAGS.batch_size, dataset.num_classes])
            total_labels = np.empty([num_batches * FLAGS.batch_size], dtype=np.int32)
            offset = 0

            with tf.Session() as sess:
                coord = tf.train.Coordinator()
                saver = tf.train.Saver()
                saver.restore(sess, checkpoint_paths[index])
                threads = tf.train.start_queue_runners(sess=sess, coord=coord)
                for i in range(num_batches):
                    print('step: %d/%d' % (i, num_batches))
                    o, l = sess.run([logits, labels])
                    total_output[offset:offset + FLAGS.batch_size] = o
                    total_labels[offset:offset + FLAGS.batch_size] = l
                    offset += FLAGS.batch_size
                coord.request_stop()
                coord.join(threads)

            output_list.append(total_output)
            labels_list.append(total_labels)

    total_count = num_batches * FLAGS.batch_size

    for i in range(len(output_list)):
        logits = tf.cast(tf.constant(output_list[i]), dtype=tf.float32)
        predictions = tf.nn.softmax(logits)
        labels = tf.constant(labels_list[i])
        top1_op = tf.nn.in_top_k(predictions, labels, 1)
        top5_op = tf.nn.in_top_k(predictions, labels, 5)

        with tf.Session() as sess:
            top1, top5 = sess.run([top1_op, top5_op])

        print('Top 1 accuracy: %f' % (np.sum(top1) / float(total_count)))
        print('Top 5 accuracy: %f' % (np.sum(top5) / float(total_count)))

    output_sum = tf.zeros([total_count, dataset.num_classes])
    for output in output_list:
        logits = tf.cast(tf.constant(output), dtype=tf.float32)
        output_sum += logits
    output_sum /= len(output_list)

    predictions = tf.nn.softmax(output_sum)
    labels = tf.constant(labels_list[0])
    top1_op = tf.nn.in_top_k(predictions, labels, 1)
    top5_op = tf.nn.in_top_k(predictions, labels, 5)

    with tf.Session() as sess:
        top1, top5 = sess.run([top1_op, top5_op])

    print('Top 1 accuracy: %f' % (np.sum(top1) / float(total_count)))
    print('Top 5 accuracy: %f' % (np.sum(top5) / float(total_count)))

if __name__ == '__main__':
  tf.app.run()