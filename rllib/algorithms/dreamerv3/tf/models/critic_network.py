"""
[1] Mastering Diverse Domains through World Models - 2023
D. Hafner, J. Pasukonis, J. Ba, T. Lillicrap
https://arxiv.org/pdf/2301.04104v1.pdf
"""
from typing import Optional

import tensorflow as tf

from ray.rllib.algorithms.dreamerv3.tf.models.components.mlp import MLP
from ray.rllib.algorithms.dreamerv3.tf.models.components.reward_predictor_layer import (
    RewardPredictorLayer,
)


class CriticNetwork(tf.keras.Model):
    """The critic network described in [1], predicting values for policy learning.

    Contains a copy of itself (EMA net) for weight regularization.
    The EMA net is updated after each train step via EMA (using the `ema_decay`
    parameter and the actual critic's weights). The EMA net is NOT used for target
    computations (we use the actual critic for that), its only purpose is to compute a
    weights regularizer term for the critic's loss such that the actual critic does not
    move too quickly.
    """

    def __init__(
        self,
        *,
        model_dimension: Optional[str] = "XS",
        num_buckets: int = 255,
        lower_bound: float = -20.0,
        upper_bound: float = 20.0,
        ema_decay: float = 0.98,
    ):
        """Initializes a CriticNetwork instance.

        Args:
            model_dimension: The "Model Size" used according to [1] Appendinx B.
               Use None for manually setting the different network sizes.
            num_buckets: The number of buckets to create. Note that the number of
                possible symlog'd outcomes from the used distribution is
                `num_buckets` + 1:
                lower_bound --bucket-- o[1] --bucket-- o[2] ... --bucket-- upper_bound
                o=outcomes
                lower_bound=o[0]
                upper_bound=o[num_buckets]
            lower_bound: The symlog'd lower bound for a possible reward value.
                Note that a value of -20.0 here already allows individual (actual env)
                rewards to be as low as -400M. Buckets will be created between
                `lower_bound` and `upper_bound`.
            upper_bound: The symlog'd upper bound for a possible reward value.
                Note that a value of +20.0 here already allows individual (actual env)
                rewards to be as high as 400M. Buckets will be created between
                `lower_bound` and `upper_bound`.
            ema_decay: The weight to use for updating the weights of the critic's copy
                vs the actual critic. After each training update, the EMA copy of the
                critic gets updated according to:
                ema_net=(`ema_decay`*ema_net) + (1.0-`ema_decay`)*critic_net
                The EMA copy of the critic is used inside the critic loss function only
                to produce a regularizer term against the current critic's weights, NOT
                to compute any target values.
        """
        super().__init__(name="critic")

        self.model_dimension = model_dimension
        self.ema_decay = ema_decay

        # "Fast" critic network(s) (mlp + reward-pred-layer). This is the network
        # we actually train with our critic loss.
        # IMPORTANT: We also use this to compute the return-targets, BUT we regularize
        # the critic loss term such that the weights of this fast critic stay close
        # to the EMA weights (see below).
        self.mlp = MLP(
            model_dimension=self.model_dimension,
            output_layer_size=None,
        )
        self.return_layer = RewardPredictorLayer(
            num_buckets=num_buckets,
            lower_bound=lower_bound,
            upper_bound=upper_bound,
        )

        # Weights-EMA (EWMA) containing networks for critic loss (similar to a
        # target net, BUT not used to compute anything, just for the
        # weights regularizer term inside the critic loss).
        self.mlp_ema = MLP(
            model_dimension=self.model_dimension,
            output_layer_size=None,
            trainable=False,
        )
        self.return_layer_ema = RewardPredictorLayer(
            num_buckets=num_buckets,
            lower_bound=lower_bound,
            upper_bound=upper_bound,
            trainable=False,
        )

    def call(self, h, z, return_logits=False, use_ema=False):
        """Performs a forward pass through the critic network.

        Args:
            h: The deterministic hidden state of the sequence model. [B, dim(h)].
            z: The stochastic discrete representations of the original
                observation input. [B, num_categoricals, num_classes].
            return_logits: Whether also return (as a second tuple item) the logits
                computed by the binned return layer (instead of only the value itself).
            use_ema: Whether to use the EMA-copy of the critic instead of the actual
                critic to perform this computation.
        """
        # Flatten last two dims of z.
        assert len(z.shape) == 3
        z_shape = tf.shape(z)
        z = tf.reshape(tf.cast(z, tf.float32), shape=(z_shape[0], -1))
        assert len(z.shape) == 2
        out = tf.concat([h, z], axis=-1)

        if not use_ema:
            # Send h-cat-z through MLP.
            out = self.mlp(out)
            # Return expected return OR (expected return, probs of bucket values).
            return self.return_layer(out, return_logits=return_logits)
        else:
            out = self.mlp_ema(out)
            return self.return_layer_ema(out, return_logits=return_logits)

    def init_ema(self) -> None:
        """Initializes the EMA-copy of the critic from the critic's weights.

        After calling this method, the two networks have identical weights.
        """
        vars = self.mlp.trainable_variables + self.return_layer.trainable_variables
        vars_ema = self.mlp_ema.variables + self.return_layer_ema.variables
        assert len(vars) == len(vars_ema)
        for var, var_ema in zip(vars, vars_ema):
            var_ema.assign(var)

    def update_ema(self) -> None:
        """Updates the EMA-copy of the critic according to the update formula:

        ema_net=(`ema_decay`*ema_net) + (1.0-`ema_decay`)*critic_net
        """
        vars = self.mlp.trainable_variables + self.return_layer.trainable_variables
        vars_ema = self.mlp_ema.variables + self.return_layer_ema.variables
        assert len(vars) == len(vars_ema)
        for var, var_ema in zip(vars, vars_ema):
            var_ema.assign(self.ema_decay * var_ema + (1.0 - self.ema_decay) * var)
