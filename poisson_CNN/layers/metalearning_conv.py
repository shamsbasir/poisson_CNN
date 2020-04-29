import tensorflow as tf
from collections.abc import Iterable

def convolution_and_bias_add_closure(data_format, conv_method, use_bias):
    if use_bias:
        @tf.function
        def single_sample_convolution_and_bias_add(conv_input, kernel, bias):
            conv_input = tf.expand_dims(conv_input, 0)
            output = conv_method(conv_input, kernel, padding = "VALID", data_format = data_format)
            output = tf.nn.bias_add(output, bias, data_format = data_format)
            return output[0,...]

        @tf.function
        def convolution_with_batched_kernels(conv_input, kernels, biases):
            batch_size = tf.keras.backend.shape(conv_input)[0]
            output = tf.map_fn(lambda x: single_sample_convolution_and_bias_add(x[0],x[1],x[2]), [conv_input, kernels, biases], dtype = tf.keras.backend.floatx())
            return output
    else:
        @tf.function
        def single_sample_convolution(conv_input, kernel):
            conv_input = tf.expand_dims(conv_input, 0)
            output = conv_method(conv_input, kernel, padding = "VALID", data_format = data_format)
            return output[0,...]

        @tf.function
        def convolution_with_batched_kernels(conv_input, kernels):
            batch_size = tf.keras.backend.shape(conv_input)[0]
            output = tf.map_fn(lambda x: single_sample_convolution(x[0],x[1]), [conv_input, kernels], dtype = tf.keras.backend.floatx())
            return output
        
    return convolution_with_batched_kernels

def convert_keras_dataformat_to_tf(df,ndims):
    if df == 'channels_first':
        return 'NC'+'DHW'[3-ndims:]
    elif df == 'channels_last':
        return 'N'+'DHW'[3-ndims:]+'C'
    else:
        raise(ValueError('Unrecognized data format - must be channels_first or channels_last'))


class metalearning_conv(tf.keras.models.Model):
    def __init__(self, previous_layer_filters, filters, kernel_size, strides=None, padding = 'valid', padding_mode = 'constant', constant_padding_value = 0.0, data_format = 'channels_first', dilation_rate = None, conv_activation = tf.keras.activations.linear, use_bias = True, kernel_initializer = None, bias_initializer = None, kernel_regularizer = None, bias_regularizer = None, activity_regularizer = None, kernel_constraint = None, bias_constraint = None, dimensions = None, dense_activations = tf.keras.activations.linear, pre_output_dense_units = [8,16], **kwargs):

        super().__init__(**kwargs)
        
        if dimensions is None:
            self.dimensions = len(filters)
        else:
            self.dimensions = dimensions
        
        if isinstance(kernel_size, int):
            self.kernel_size = [kernel_size for _ in range(self.dimensions)]

        if dilation_rate is None:
            self.dilation_rate = [1 for _ in range(self.dimensions)]
        else:
            self.dilation_rate = dilation_rate

        if strides is None:
            self.strides = [1 for _ in range(self.dimensions)]
        else:
            self.strides = strides
        print(self.strides)

        self.conv_activation = conv_activation
        
        self.data_format = data_format
        self._tf_data_format = convert_keras_dataformat_to_tf(self.data_format, self.dimensions)
        
        self.filters = filters
        self.previous_layer_filters = previous_layer_filters
        
        self.use_bias = use_bias

        self.padding = padding
        self.padding_mode = padding_mode

        if self.padding.upper() == 'SAME':#build custom paddings
            self.padding_sizes = [[0,0]] + [[ks//2,ks//2 if ks%2==1 else ks//2-1] for ks in self.kernel_size]
            if self._tf_data_format[1] == 'C':
                self.padding_sizes.insert(1,[0,0])
            else:
                self.padding_sizes.append([0,0])
            print(self.padding_sizes)
        self.constant_padding_value = constant_padding_value
        
        self.kernel_shape = tf.concat([self.kernel_size,[self.previous_layer_filters],[self.filters]], axis = 0)
        self.bias_shape = tf.constant([self.filters])
        
        if callable(dense_activations):
            dense_activations = [dense_activations for _ in range(len(pre_output_dense_units)+1)]

        dense_layer_args = {'use_bias': use_bias, 'kernel_initializer': kernel_initializer, 'bias_initializer': bias_initializer, 'kernel_regularizer': kernel_regularizer, 'bias_regularizer': bias_regularizer, 'activity_regularizer': activity_regularizer, 'kernel_constraint': kernel_constraint, 'bias_constraint': bias_constraint}
            
        self.dense_layers = [tf.keras.layers.Dense(pre_output_dense_units[k], activation = dense_activations[k], **dense_layer_args) for k in range(len(pre_output_dense_units))] + [tf.keras.layers.Dense(tf.reduce_prod(self.kernel_shape)+self.bias_shape, activation = dense_activations[-1], **dense_layer_args)]

        if self.dimensions == 1:
            self.conv_method = lambda *args, **kwargs: tf.nn.conv1d(*args, strides = self.strides, dilations = self.dilation_rate, **kwargs)#tf.keras.backend.conv1d
        elif self.dimensions == 2:
            #self.conv_method = lambda *args, **kwargs: tf.keras.backend.conv2d(*args, strides = self.strides, dilation_rate = self.dilation_rate, **kwargs)
            self.conv_method = lambda *args, **kwargs: tf.nn.conv2d(*args, strides = self.strides, dilations = self.dilation_rate, **kwargs)
        elif self.dimensions == 3:
            self.conv_method = lambda *args, **kwargs: tf.nn.conv3d(*args, strides = self.strides, dilations = self.dilation_rate, **kwargs)
        else:
            raise(ValueError('dimensions must be 1,2 or 3'))
        
        self.conv_method = convolution_and_bias_add_closure(self._tf_data_format, self.conv_method, self.use_bias)

    @tf.function
    def call(self, inputs):

        conv_input = inputs[0]
        dense_input = inputs[1]

        conv_kernel_and_bias = self.dense_layers[0](dense_input)
        for layer in self.dense_layers[1:]:
            conv_kernel_and_bias = layer(conv_kernel_and_bias)

        conv_kernel = tf.reshape(conv_kernel_and_bias[:,:tf.reduce_prod(self.kernel_shape)],tf.concat([[conv_kernel_and_bias.shape[0]],self.kernel_shape], axis = 0))

        if self.padding.upper() == 'SAME':
            conv_input = tf.pad(conv_input, self.padding_sizes, mode=self.padding_mode, constant_values=self.constant_padding_value)

        if self.use_bias:
            bias = conv_kernel_and_bias[:,-tf.squeeze(self.bias_shape):]
            output = self.conv_method(conv_input, conv_kernel, bias)
        else:
            output = self.conv_method(conv_input, conv_kernel)

        return self.conv_activation(output)

if __name__ == '__main__':
    mod = metalearning_conv(2, 5, 5, conv_activation = tf.nn.relu, dimensions = 2, data_format = 'channels_first', padding='same', padding_mode='SYMMETRIC', use_bias = True)
    convinp = tf.random.uniform((10,2,100,100))
    denseinp = 2*tf.random.uniform((10,5))-1

    #print(mod([convinp,denseinp]))
    res = mod([convinp,denseinp])
    print(res.shape)
    import time
    t0 = time.time()
    ntrials = 100
    for k in range(ntrials):
        q = mod([convinp,denseinp])
    print((time.time()-t0)/ntrials)