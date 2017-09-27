import random
import tensorflow as tf
from tensorflow.python.framework import function
from .layers import dilated_conv2d, layer_norm


def fn_with_custom_grad(grad_fn, use_global_vars=False):
    """Decorator to create a subgraph with a custom gradient function.

    The subgraph created by the decorated function is NOT put in a Defun and so
    does not suffer from the limitations of the Defun (all subgraph ops on the
    same device, no summaries).

    Args:
        grad_fn: function with signature
          (inputs, variables, outputs, output_grads) -> (grad_inputs, grad_vars),
           all of which are lists of Tensors.
        use_global_vars: if True, variables will be the global variables created.
            If False, will be the trainable variables.

    Returns:
        Decorator for function such that the gradient is defined by grad_fn.
    """

    def dec(fn):

        def wrapped(*args):
            return _fn_with_custom_grad(fn, args, grad_fn, use_global_vars=use_global_vars)

        return wrapped

    return dec


def _fn_with_custom_grad(fn, inputs, grad_fn, use_global_vars=False):
    """Create a subgraph with a custom gradient.

    Args:
        fn: function that takes inputs as arguments and produces 1 or more Tensors.
        inputs: list<Tensor>, will be passed as fn(*inputs).
        grad_fn: function with signature
            (inputs, vars, outputs, output_grads) -> (grad_inputs, grad_vars),
            all of which are lists of Tensors.
        use_global_vars: if True, variables will be the global variables created.
           If False, will be the trainable variables.

    Returns:
        fn(*inputs)
    """
    with tf.variable_scope(None, default_name="fn_with_custom_grad") as vs:
        inputs = list(inputs)
        outputs = fn(*inputs)
        if use_global_vars:
            train_vars = list(vs.global_variables())
        else:
            train_vars = list(vs.trainable_variables())

    if grad_fn is None:
        return outputs
    else:
        if not (isinstance(outputs, tuple) or isinstance(outputs, list)):
            outputs = [outputs]
        outputs = list(outputs)

        in_types = [t.dtype for t in inputs]
        out_types = [t.dtype for t in outputs]
        var_types = [t.dtype for t in train_vars]

        def custom_grad_fn(op, *dys):
            """Custom grad fn applying grad_fn for identity Defun."""
            dys = list(dys)
            fn_inputs = op.inputs[:len(inputs)]
            fn_vars = op.inputs[len(inputs):len(inputs) + len(train_vars)]
            fn_outputs = op.inputs[len(inputs) + len(train_vars):]
            assert len(fn_outputs) == len(outputs)
            assert len(fn_outputs) == len(dys)

            grad_inputs, grad_vars = grad_fn(
                fn_inputs, fn_vars, fn_outputs, dys)
            grad_outputs = [None] * len(fn_outputs)
            return tuple(grad_inputs + grad_vars + grad_outputs)

        # The Defun takes as input the original inputs, the trainable variables
        # created in fn, and the outputs. In the forward it passes through the
        # outputs. In the backwards, it produces gradients for the original inputs
        # and the trainable variables.
        @function.Defun(
            *(in_types + var_types + out_types),
            func_name="identity_custom_grad%d" % random.randint(1, 10**9),
            python_grad_func=custom_grad_fn,
            shape_func=lambda _: [t.get_shape() for t in outputs])
        def identity(*args):
            outs = args[len(inputs) + len(train_vars):]
            return tuple([tf.identity(t) for t in outs])

        id_out = identity(*(inputs + train_vars + outputs))
        return id_out


def format_input_left_padding(inputs, **kwargs):
    static_shape = inputs.get_shape()
    if not static_shape or len(static_shape) != 4:
        raise ValueError(
            "Inputs to conv must have statically known rank 4. Shape: " + str(static_shape))
    dilation_rate = (1, 1)
    assert kwargs['filter_size'] is not None
    filter_size = kwargs['filter_size']
    if isinstance(filter_size, int):
        filter_size = [filter_size, filter_size]
    if "dilation_rate" in kwargs:
        dilation_rate = kwargs["dilation_rate"]
    assert filter_size[0] % 2 == 1 and filter_size[1] % 2 == 1
    height_padding = 2 * (filter_size[0] // 2) * dilation_rate[0]
    cond_padding = tf.cond(
        tf.equal(tf.shape(inputs)[2], 1), lambda: tf.constant(0),
        lambda: tf.constant(2 * (filter_size[1] // 2) * dilation_rate[1]))
    width_padding = 0 if static_shape[2] == 1 else cond_padding
    padding = [[0, 0], [height_padding, 0], [width_padding, 0], [0, 0]]
    inputs = tf.pad(inputs, padding)
    # Set middle two dimensions to None to prevent convolution from complaining
    inputs.set_shape([static_shape[0], None, None, static_shape[3]])
    kwargs["padding"] = "VALID"
    return inputs, kwargs


def saturating_sigmoid(x):
    """Saturating sigmoid: 1.2 * sigmoid(x) - 0.1 cut to [0, 1]."""
    with tf.name_scope("saturating_sigmoid", [x]):
        y = tf.sigmoid(x)
        return tf.minimum(1.0, tf.maximum(0.0, 1.2 * y - 0.1))


def conv2d_v2(inputs, n_output_channels, is_training, reuse, **kwargs):
    """Adds a 2D dilated convolutional layer

        also known as convolution with holes or atrous convolution.
        If the rate parameter is equal to one, it performs regular 2-D convolution.
        If the rate parameter
        is greater than one, it performs convolution with holes, sampling the input
        values every rate pixels in the height and width dimensions.
        `convolutional layer` creates a variable called `weights`, representing a conv
        weight matrix, which is multiplied by the `x` to produce a
        `Tensor` of hidden units. If a `batch_norm` is provided (such as
        `batch_norm`), it is then applied. Otherwise, if `batch_norm` is
        None and a `b_init` and `use_bias` is provided then a `biases` variable would be
        created and added the hidden units. Finally, if `activation` is not `None`,
        it is applied to the hidden units as well.
        Note: that if `x` have a rank 4

    Args:
        x: A 4-D `Tensor` of with rank 4 and value for the last dimension,
            i.e. `[batch_size, in_height, in_width, depth]`,
        is_training: Bool, training or testing
        n_output: Integer or long, the number of output units in the layer.
        reuse: whether or not the layer and its variables should be reused. To be
            able to reuse the layer scope must be given.
        filter_size: a int or list/tuple of 2 positive integers specifying the spatial
        dimensions of of the filters.
        dilation:  A positive int32. The stride with which we sample input values across
            the height and width dimensions. Equivalently, the rate by which we upsample the
            filter values by inserting zeros across the height and width dimensions. In the literature,
            the same parameter is sometimes called input stride/rate or dilation.
        padding: one of `"VALID"` or `"SAME"`. IF padding is LEFT, it preprocess the input to use Valid padding
        activation: activation function, set to None to skip it and maintain
            a linear activation.
        batch_norm: normalization function to use. If
            `batch_norm` is `True` then google original implementation is used and
            if another function is provided then it is applied.
            default set to None for no normalizer function
        batch_norm_args: normalization function parameters.
        w_init: An initializer for the weights.
        w_regularizer: Optional regularizer for the weights.
        untie_biases: spatial dimensions wise baises
        b_init: An initializer for the biases. If None skip biases.
        outputs_collections: The collections to which the outputs are added.
        trainable: If `True` also add variables to the graph collection
            `GraphKeys.TRAINABLE_VARIABLES` (see tf.Variable).
        name: Optional name or scope for variable_scope/name_scope.
        use_bias: Whether to add bias or not

    Returns:
        The 4-D `Tensor` variable representing the result of the series of operations.
        e.g.: 4-D `Tensor` [batch, new_height, new_width, n_output].

    Raises:
        ValueError: if x has rank less than 4 or if its last dimension is not set.
    """
    if 'padding' in kwargs and kwargs['padding'] == 'LEFT':
        inputs, kwargs = format_input_left_padding(inputs, **kwargs)
    return dilated_conv2d(inputs, n_output_channels, is_training, reuse, **kwargs)


def conv2d_gru(inputs, n_output_channels, is_training, reuse, filter_size=3, padding="SAME", dilation=1, name='conv2d_gru', **kwargs):
    """Adds a convolutional GRU layer in 1 dimension

    Args:
        x: A 4-D `Tensor` of with rank 4 and value for the last dimension,
            i.e. `[batch_size, in_height, in_width, depth]`,
        is_training: Bool, training or testing
        n_output: Integer or long, the number of output units in the layer.
        reuse: whether or not the layer and its variables should be reused. To be
            able to reuse the layer scope must be given.
        filter_size: a int or list/tuple of 2 positive integers specifying the spatial
        dimensions of of the filters.
        dilation:  A positive int32. The stride with which we sample input values across
            the height and width dimensions. Equivalently, the rate by which we upsample the
            filter values by inserting zeros across the height and width dimensions. In the literature,
            the same parameter is sometimes called input stride/rate or dilation.
        padding: one of `"VALID"` or `"SAME"`. IF padding is LEFT, it preprocess the input to use Valid padding
        activation: activation function, set to None to skip it and maintain
            a linear activation.
        batch_norm: normalization function to use. If
            `batch_norm` is `True` then google original implementation is used and
            if another function is provided then it is applied.
            default set to None for no normalizer function
        batch_norm_args: normalization function parameters.
        w_init: An initializer for the weights.
        w_regularizer: Optional regularizer for the weights.
        untie_biases: spatial dimensions wise baises
        b_init: An initializer for the biases. If None skip biases.
        outputs_collections: The collections to which the outputs are added.
        trainable: If `True` also add variables to the graph collection
            `GraphKeys.TRAINABLE_VARIABLES` (see tf.Variable).
        name: Optional name or scope for variable_scope/name_scope.
        use_bias: Whether to add bias or not

    Returns:
        The 4-D `Tensor` variable representing the result of the series of operations.
        e.g.: 4-D `Tensor` [batch, new_height, new_width, n_output].

    Raises:
        ValueError: if x has rank less than 4 or if its last dimension is not set.
    """
    def conv2d_fn(x, name, bias_start, padding):
        return conv2d_v2(x, n_output_channels, is_training, reuse, filter_size=filter_size, padding=padding, b_init=bias_start, dilation=dilation, name=name, **kwargs)

    with tf.variable_scope(name, reuse=reuse):
        reset = saturating_sigmoid(conv2d_fn(inputs, "reset", 1.0, padding))
        gate = saturating_sigmoid(conv2d_fn(inputs, "gate", 1.0, padding))
        candidate = tf.tanh(
            conv2d_fn(reset * inputs, "candidate", 0.0, padding))
        return gate * inputs + (1 - gate) * candidate


def conv2d_lstm(inputs, n_output_channels, is_training, reuse, filter_size=3, padding="SAME", dilation=1, name='conv2d_gru', **kwargs):
    """Adds a convolutional LSTM layer in 1 dimension

    Args:
        x: A 4-D `Tensor` of with rank 4 and value for the last dimension,
            i.e. `[batch_size, in_height, in_width, depth]`,
        is_training: Bool, training or testing
        n_output: Integer or long, the number of output units in the layer.
        reuse: whether or not the layer and its variables should be reused. To be
            able to reuse the layer scope must be given.
        filter_size: a int or list/tuple of 2 positive integers specifying the spatial
        dimensions of of the filters.
        dilation:  A positive int32. The stride with which we sample input values across
            the height and width dimensions. Equivalently, the rate by which we upsample the
            filter values by inserting zeros across the height and width dimensions. In the literature,
            the same parameter is sometimes called input stride/rate or dilation.
        padding: one of `"VALID"` or `"SAME"`. IF padding is LEFT, it preprocess the input to use Valid padding
        activation: activation function, set to None to skip it and maintain
            a linear activation.
        batch_norm: normalization function to use. If
            `batch_norm` is `True` then google original implementation is used and
            if another function is provided then it is applied.
            default set to None for no normalizer function
        batch_norm_args: normalization function parameters.
        w_init: An initializer for the weights.
        w_regularizer: Optional regularizer for the weights.
        untie_biases: spatial dimensions wise baises
        b_init: An initializer for the biases. If None skip biases.
        outputs_collections: The collections to which the outputs are added.
        trainable: If `True` also add variables to the graph collection
            `GraphKeys.TRAINABLE_VARIABLES` (see tf.Variable).
        name: Optional name or scope for variable_scope/name_scope.
        use_bias: Whether to add bias or not

    Returns:
        The 4-D `Tensor` variable representing the result of the series of operations.
        e.g.: 4-D `Tensor` [batch, new_height, new_width, n_output].

    Raises:
        ValueError: if x has rank less than 4 or if its last dimension is not set.
    """
    with tf.variable_scope(name, reuse=reuse):
        gates = conv2d_v2(inputs, 4 * n_output_channels, is_training, reuse,
                          filter_size=filter_size, padding=padding, dilation=dilation, name=name, **kwargs)
        g = tf.split(layer_norm(gates, 4 * n_ouput_channels), 4, axis=3)
        new_cell = tf.sigmoid(g[0]) * x + tf.sigmoid(g[1]) * tf.tanh(g[3])
        return tf.sigmoid(g[2]) * tf.tanh(new_cell)