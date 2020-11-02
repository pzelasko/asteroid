from torch import nn
from .base_models import BaseModel
from ..filterbanks import make_enc_dec
from ..filterbanks.transforms import take_mag, take_cat
from ..masknn import norms, activations
from ..utils.torch_utils import pad_x_to_y


class DeMask(BaseModel):  # CHECK-JIT
    """
    Simple MLP model for surgical mask speech enhancement A transformed-domain masking approach is used.
    Args:
        input_type (str, optional): whether the magnitude spectrogram "mag" or both real imaginary parts "reim" are
                    passed as features to the masker network.
                    Concatenation of "mag" and "reim" also can be used by using "cat".
        output_type (str, optional): whether the masker ouputs a mask
                    for magnitude spectrogram "mag" or both real imaginary parts "reim".

        hidden_dims (list, optional): list of MLP hidden layer sizes.
        dropout (float, optional): dropout probability.
        activation (str, optional): type of activation used in hidden MLP layers.
        mask_act (str, optional): Which non-linear function to generate mask.
        norm_type (str, optional): To choose from ``'BN'``, ``'gLN'``,
            ``'cLN'``.

        fb_name (str): type of analysis and synthesis filterbanks used,
                            choose between ["stft", "free", "analytic_free"].
        n_filters (int): number of filters in the analysis and synthesis filterbanks.
        stride (int): filterbank filters stride.
        kernel_size (int): length of filters in the filterbank.
        encoder_activation (str)
        sample_rate (float): Sampling rate of the model.
        **fb_kwargs (dict): Additional kwards to pass to the filterbank
            creation.
    """

    def __init__(
        self,
        input_type="mag",
        output_type="mag",
        hidden_dims=[1024],
        dropout=0.0,
        activation="relu",
        mask_act="relu",
        norm_type="gLN",
        fb_type="stft",
        n_filters=512,
        stride=256,
        kernel_size=512,
        sample_rate=16000,
        **fb_kwargs,
    ):

        super().__init__()
        self.input_type = input_type
        self.output_type = output_type
        self.hidden_dims = hidden_dims
        self.dropout = dropout
        self.activation = activation
        self.mask_act = mask_act
        self.norm_type = norm_type
        self.fb_type = fb_type
        self.n_filters = n_filters
        self.stride = stride
        self.kernel_size = kernel_size
        self.fb_kwargs = fb_kwargs
        self._sample_rate = sample_rate

        self.encoder, self.decoder = make_enc_dec(
            fb_type,
            kernel_size=kernel_size,
            n_filters=n_filters,
            stride=stride,
            sample_rate=sample_rate,
            **fb_kwargs,
        )

        net = self._build_masker_nn()
        self.masker = nn.Sequential(*net)

    def _build_masker_nn(self):
        n_feats_input = self._get_n_feats_input()
        make_layer_norm = norms.get(self.norm_type)
        net = [make_layer_norm(n_feats_input)]
        layer_activation = activations.get(self.activation)()
        in_chan = n_feats_input
        for hidden_dim in self.hidden_dims:
            net.extend(
                [
                    nn.Conv1d(in_chan, hidden_dim, 1),
                    make_layer_norm(hidden_dim),
                    layer_activation,
                    nn.Dropout(self.dropout),
                ]
            )
            in_chan = hidden_dim

        n_feats_output = self._get_n_feats_output()
        net.extend([nn.Conv1d(in_chan, n_feats_output, 1), activations.get(self.mask_act)()])
        return net

    def _get_n_feats_input(self):
        if self.input_type == "reim":
            return self.encoder.n_feats_out

        if self.input_type not in {"mag", "cat"}:
            raise NotImplementedError("Input type should be either mag, reim or cat")

        n_feats_input = self.encoder.n_feats_out // 2
        if self.input_type == "cat":
            n_feats_input += self.encoder.n_feats_out
        return n_feats_input

    def _get_n_feats_output(self):
        if self.output_type == "mag":
            return self.encoder.n_feats_out // 2
        if self.output_type == "reim":
            return self.encoder.n_feats_out
        raise NotImplementedError("Output type should be either mag or reim")

    def forward(self, wav):

        # Handle 1D, 2D or n-D inputs
        was_one_d = False
        if wav.ndim == 1:
            was_one_d = True
            wav = wav.unsqueeze(0).unsqueeze(1)
        if wav.ndim == 2:
            wav = wav.unsqueeze(1)
        # Real forward
        tf_rep = self.encoder(wav)

        mask_in = self.preprocess_masker_input(tf_rep)
        est_masks = self.masker(mask_in)
        est_masks = self.postprocess_masks(est_masks)
        tf_rep = self.preprocess_product_input(tf_rep)
        masked_tf_rep = est_masks * tf_rep

        out_wavs = pad_x_to_y(self.decoder(masked_tf_rep), wav)
        if was_one_d:
            return out_wavs.squeeze(0)
        return out_wavs

    def preprocess_masker_input(self, tf_rep):
        if self.input_type == "mag":
            return take_mag(tf_rep)
        if self.input_type == "cat":
            return take_cat(tf_rep)
        # No need for NotImplementedError as input_type checked at init
        return tf_rep

    def preprocess_product_input(self, tf_rep):
        if self.output_type == "mag":
            return tf_rep
        return tf_rep.unsqueeze(1)

    def postprocess_masks(self, est_masks):
        if self.output_type == "mag":
            return est_masks.repeat(1, 2, 1)
        # No need for invalid output_types as checked at init
        return est_masks

    @property
    def sample_rate(self):
        return self._sample_rate

    def get_model_args(self):
        """ Arguments needed to re-instantiate the model. """
        model_args = {
            "input_type": self.input_type,
            "output_type": self.output_type,
            "hidden_dims": self.hidden_dims,
            "dropout": self.dropout,
            "activation": self.activation,
            "mask_act": self.mask_act,
            "norm_type": self.norm_type,
            "fb_type": self.fb_type,
            "n_filters": self.n_filters,
            "stride": self.stride,
            "kernel_size": self.kernel_size,
            "fb_kwargs": self.fb_kwargs,
            "sample_rate": self._sample_rate,
        }
        model_args.update(self.fb_kwargs)
        return model_args
