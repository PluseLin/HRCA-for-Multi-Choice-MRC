
# coding=utf-8
# Copyright 2018 Google AI, Google Brain and the HuggingFace Inc. team.
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
"""PyTorch ALBERT model. """

import os
import math
import logging
import torch
import torch.nn as nn
from torch.nn import CrossEntropyLoss, MSELoss
from transformers.modeling_utils import PreTrainedModel
from transformers.configuration_albert import AlbertConfig
from transformers.modeling_bert import BertEmbeddings, BertSelfAttention, prune_linear_layer, ACT2FN
from .file_utils import add_start_docstrings

logger = logging.getLogger(__name__)


ALBERT_PRETRAINED_MODEL_ARCHIVE_MAP = {
    # 'albert-base-v2': "{path}/albert-base-v2-pytorch_model.bin",
    'albert-xxlarge-v2': "{path}/albert-xxlarge-v2-pytorch_model.bin",
    'albert-base-v1': "https://s3.amazonaws.com/models.huggingface.co/bert/albert-base-pytorch_model.bin",
    'albert-large-v1': "https://s3.amazonaws.com/models.huggingface.co/bert/albert-large-pytorch_model.bin",
    'albert-xlarge-v1': "https://s3.amazonaws.com/models.huggingface.co/bert/albert-xlarge-pytorch_model.bin",
    'albert-xxlarge-v1': "https://s3.amazonaws.com/models.huggingface.co/bert/albert-xxlarge-pytorch_model.bin",
    'albert-base-v2': "https://s3.amazonaws.com/models.huggingface.co/bert/albert-base-v2-pytorch_model.bin",
    'albert-large-v2': "https://s3.amazonaws.com/models.huggingface.co/bert/albert-large-v2-pytorch_model.bin",
    # 'albert-xlarge-v2': "{path}}albert-xlarge-v2-pytorch_model.bin",
    # 'albert-xxlarge-v2': "https://s3.amazonaws.com/models.huggingface.co/bert/albert-xxlarge-v2-pytorch_model.bin",
    # 'albert-large-v2-local':"albert_large/pytorch_model.bin"
}


def load_tf_weights_in_albert(model, config, tf_checkpoint_path):
    """ Load tf checkpoints in a pytorch model."""
    try:
        import re
        import numpy as np
        import tensorflow as tf
    except ImportError:
        logger.error("Loading a TensorFlow model in PyTorch, requires TensorFlow to be installed. Please see "
            "https://www.tensorflow.org/install/ for installation instructions.")
        raise
    tf_path = os.path.abspath(tf_checkpoint_path)
    logger.info("Converting TensorFlow checkpoint from {}".format(tf_path))
    # Load weights from TF model
    init_vars = tf.train.list_variables(tf_path)
    names = []
    arrays = []
    for name, shape in init_vars:
        logger.info("Loading TF weight {} with shape {}".format(name, shape))
        array = tf.train.load_variable(tf_path, name)
        names.append(name)
        arrays.append(array)

    for name, array in zip(names, arrays):
        print(name)
    
    for name, array in zip(names, arrays):
        original_name = name

        # If saved from the TF HUB module
        name = name.replace("module/", "")

        # Renaming and simplifying
        name = name.replace("ffn_1", "ffn")
        name = name.replace("bert/", "albert/")
        name = name.replace("attention_1", "attention")   
        name = name.replace("transform/", "")
        name = name.replace("LayerNorm_1", "full_layer_layer_norm")    
        name = name.replace("LayerNorm", "attention/LayerNorm")   
        name = name.replace("transformer/", "")

        # The feed forward layer had an 'intermediate' step which has been abstracted away
        name = name.replace("intermediate/dense/", "")
        name = name.replace("ffn/intermediate/output/dense/", "ffn_output/")

        # ALBERT attention was split between self and output which have been abstracted away
        name = name.replace("/output/", "/")
        name = name.replace("/self/", "/")

        # The pooler is a linear layer
        name = name.replace("pooler/dense", "pooler")

        # The classifier was simplified to predictions from cls/predictions
        name = name.replace("cls/predictions", "predictions")
        name = name.replace("predictions/attention", "predictions")

        # Naming was changed to be more explicit
        name = name.replace("embeddings/attention", "embeddings")    
        name = name.replace("inner_group_", "albert_layers/") 
        name = name.replace("group_", "albert_layer_groups/")   

        # Classifier
        if len(name.split("/")) == 1 and ("output_bias" in name or "output_weights" in name):
            name = "classifier/" + name

        # No ALBERT model currently handles the next sentence prediction task 
        if "seq_relationship" in name:
            continue

        name = name.split('/')

        # Ignore the gradients applied by the LAMB/ADAM optimizers.
        if "adam_m" in name or "adam_v" in name or "global_step" in name:
            logger.info("Skipping {}".format("/".join(name)))
            continue

        pointer = model
        for m_name in name:
            if re.fullmatch(r'[A-Za-z]+_\d+', m_name):
                l = re.split(r'_(\d+)', m_name)
            else:
                l = [m_name]

            if l[0] == 'kernel' or l[0] == 'gamma':
                pointer = getattr(pointer, 'weight')
            elif l[0] == 'output_bias' or l[0] == 'beta':
                pointer = getattr(pointer, 'bias')
            elif l[0] == 'output_weights':
                pointer = getattr(pointer, 'weight')
            elif l[0] == 'squad':
                pointer = getattr(pointer, 'classifier')
            else:
                try:
                    pointer = getattr(pointer, l[0])
                except AttributeError:
                    logger.info("Skipping {}".format("/".join(name)))
                    continue
            if len(l) >= 2:
                num = int(l[1])
                pointer = pointer[num]

        if m_name[-11:] == '_embeddings':
            pointer = getattr(pointer, 'weight')
        elif m_name == 'kernel':
            array = np.transpose(array)
        try:
            assert pointer.shape == array.shape
        except AssertionError as e:
            e.args += (pointer.shape, array.shape)
            raise
        print("Initialize PyTorch weight {} from {}".format(name, original_name))
        pointer.data = torch.from_numpy(array)

    return model


class AlbertEmbeddings(BertEmbeddings):
    """
    Construct the embeddings from word, position and token_type embeddings.
    """
    def __init__(self, config):
        super(AlbertEmbeddings, self).__init__(config)

        self.word_embeddings = nn.Embedding(config.vocab_size, config.embedding_size, padding_idx=0)
        self.position_embeddings = nn.Embedding(config.max_position_embeddings, config.embedding_size)
        self.token_type_embeddings = nn.Embedding(config.type_vocab_size, config.embedding_size)
        self.LayerNorm = torch.nn.LayerNorm(config.embedding_size, eps=config.layer_norm_eps)


class AlbertAttention(BertSelfAttention):
    def __init__(self, config):
        super(AlbertAttention, self).__init__(config)

        self.output_attentions = config.output_attentions
        self.num_attention_heads = config.num_attention_heads
        self.hidden_size = config.hidden_size 
        self.attention_head_size = config.hidden_size // config.num_attention_heads
        self.dropout = nn.Dropout(config.attention_probs_dropout_prob)
        self.dense = nn.Linear(config.hidden_size, config.hidden_size)
        self.LayerNorm = nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)
        self.pruned_heads = set()

    def prune_heads(self, heads):
        if len(heads) == 0:
            return
        mask = torch.ones(self.num_attention_heads, self.attention_head_size)
        heads = set(heads) - self.pruned_heads  # Convert to set and emove already pruned heads
        for head in heads:
            # Compute how many pruned heads are before the head and move the index accordingly
            head = head - sum(1 if h < head else 0 for h in self.pruned_heads)
            mask[head] = 0
        mask = mask.view(-1).contiguous().eq(1)
        index = torch.arange(len(mask))[mask].long()

        # Prune linear layers
        self.query = prune_linear_layer(self.query, index)
        self.key = prune_linear_layer(self.key, index)
        self.value = prune_linear_layer(self.value, index)
        self.dense = prune_linear_layer(self.dense, index, dim=1)

        # Update hyper params and store pruned heads
        self.num_attention_heads = self.num_attention_heads - len(heads)
        self.all_head_size = self.attention_head_size * self.num_attention_heads
        self.pruned_heads = self.pruned_heads.union(heads)

    def forward(self, input_ids, attention_mask=None, head_mask=None):
        mixed_query_layer = self.query(input_ids)
        mixed_key_layer = self.key(input_ids)
        mixed_value_layer = self.value(input_ids)

        query_layer = self.transpose_for_scores(mixed_query_layer)
        key_layer = self.transpose_for_scores(mixed_key_layer)
        value_layer = self.transpose_for_scores(mixed_value_layer)

        # Take the dot product between "query" and "key" to get the raw attention scores.
        attention_scores = torch.matmul(query_layer, key_layer.transpose(-1, -2))
        attention_scores = attention_scores / math.sqrt(self.attention_head_size)
        if attention_mask is not None:
            # Apply the attention mask is (precomputed for all layers in BertModel forward() function)
            attention_scores = attention_scores + attention_mask

        # Normalize the attention scores to probabilities.
        attention_probs = nn.Softmax(dim=-1)(attention_scores)

        # This is actually dropping out entire tokens to attend to, which might
        # seem a bit unusual, but is taken from the original Transformer paper.
        attention_probs = self.dropout(attention_probs)

        # Mask heads if we want to
        if head_mask is not None:
            attention_probs = attention_probs * head_mask

        context_layer = torch.matmul(attention_probs, value_layer)

        context_layer = context_layer.permute(0, 2, 1, 3).contiguous()
        new_context_layer_shape = context_layer.size()[:-2] + (self.all_head_size,)
        reshaped_context_layer = context_layer.view(*new_context_layer_shape)
        

        # Should find a better way to do this
        w = self.dense.weight.t().view(self.num_attention_heads, self.attention_head_size, self.hidden_size).to(context_layer.dtype)
        b = self.dense.bias.to(context_layer.dtype)

        projected_context_layer = torch.einsum("bfnd,ndh->bfh", context_layer, w) + b
        projected_context_layer_dropout = self.dropout(projected_context_layer)
        layernormed_context_layer = self.LayerNorm(input_ids + projected_context_layer_dropout)
        return (layernormed_context_layer, attention_probs) if self.output_attentions else (layernormed_context_layer,)


class AlbertLayer(nn.Module):
    def __init__(self, config):
        super(AlbertLayer, self).__init__()
        
        self.config = config
        self.full_layer_layer_norm = nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)
        self.attention = AlbertAttention(config)
        self.ffn = nn.Linear(config.hidden_size, config.intermediate_size) 
        self.ffn_output = nn.Linear(config.intermediate_size, config.hidden_size)
        self.activation = ACT2FN[config.hidden_act]

    def forward(self, hidden_states, attention_mask=None, head_mask=None):
        attention_output = self.attention(hidden_states, attention_mask, head_mask)
        ffn_output = self.ffn(attention_output[0])
        ffn_output = self.activation(ffn_output)
        ffn_output = self.ffn_output(ffn_output)
        hidden_states = self.full_layer_layer_norm(ffn_output + attention_output[0])

        return (hidden_states,) + attention_output[1:]  # add attentions if we output them


class AlbertLayerGroup(nn.Module):
    def __init__(self, config):
        super(AlbertLayerGroup, self).__init__()
        
        self.output_attentions = config.output_attentions
        self.output_hidden_states = config.output_hidden_states
        self.albert_layers = nn.ModuleList([AlbertLayer(config) for _ in range(config.inner_group_num)])

    def forward(self, hidden_states, attention_mask=None, head_mask=None):
        layer_hidden_states = ()
        layer_attentions = ()

        for layer_index, albert_layer in enumerate(self.albert_layers):
            layer_output = albert_layer(hidden_states, attention_mask, head_mask[layer_index])
            hidden_states = layer_output[0]

            if self.output_attentions:
                layer_attentions = layer_attentions + (layer_output[1],)

            if self.output_hidden_states:
                layer_hidden_states = layer_hidden_states + (hidden_states,)

        outputs = (hidden_states,)
        if self.output_hidden_states:
            outputs = outputs + (layer_hidden_states,)
        if self.output_attentions:
            outputs = outputs + (layer_attentions,)
        return outputs  # last-layer hidden state, (layer hidden states), (layer attentions)


class AlbertTransformer(nn.Module):
    def __init__(self, config):
        super(AlbertTransformer, self).__init__()
        
        self.config = config
        self.output_attentions = config.output_attentions
        self.output_hidden_states = config.output_hidden_states
        self.embedding_hidden_mapping_in = nn.Linear(config.embedding_size, config.hidden_size)
        self.albert_layer_groups = nn.ModuleList([AlbertLayerGroup(config) for _ in range(config.num_hidden_groups)])

    def forward(self, hidden_states, attention_mask=None, head_mask=None):
        hidden_states = self.embedding_hidden_mapping_in(hidden_states)

        all_attentions = ()

        if self.output_hidden_states:
            all_hidden_states = (hidden_states,)

        for i in range(self.config.num_hidden_layers):
            # Number of layers in a hidden group
            layers_per_group = int(self.config.num_hidden_layers / self.config.num_hidden_groups)

            # Index of the hidden group
            group_idx = int(i / (self.config.num_hidden_layers / self.config.num_hidden_groups))

            # Index of the layer inside the group
            layer_idx = int(i - group_idx * layers_per_group)
            
            layer_group_output = self.albert_layer_groups[group_idx](hidden_states, attention_mask, head_mask[group_idx*layers_per_group:(group_idx+1)*layers_per_group])  
            hidden_states = layer_group_output[0]

            if self.output_attentions:
                all_attentions = all_attentions + layer_group_output[-1]

            if self.output_hidden_states:
                all_hidden_states = all_hidden_states + (hidden_states,)

        
        outputs = (hidden_states,)
        if self.output_hidden_states:
            outputs = outputs + (all_hidden_states,)
        if self.output_attentions:
            outputs = outputs + (all_attentions,)
        return outputs  # last-layer hidden state, (all hidden states), (all attentions)



class AlbertPreTrainedModel(PreTrainedModel):
    """ An abstract class to handle weights initialization and
        a simple interface for dowloading and loading pretrained models.
    """
    config_class = AlbertConfig
    pretrained_model_archive_map = ALBERT_PRETRAINED_MODEL_ARCHIVE_MAP
    base_model_prefix = "albert"

    def _init_weights(self, module):
        """ Initialize the weights.
        """
        if isinstance(module, (nn.Linear, nn.Embedding)):
            # Slightly different from the TF version which uses truncated_normal for initialization
            # cf https://github.com/pytorch/pytorch/pull/5617
            module.weight.data.normal_(mean=0.0, std=self.config.initializer_range)
            if isinstance(module, (nn.Linear)) and module.bias is not None:
                module.bias.data.zero_()
        elif isinstance(module, nn.LayerNorm):
            module.bias.data.zero_()
            module.weight.data.fill_(1.0)


ALBERT_START_DOCSTRING = r"""    The ALBERT model was proposed in
    `ALBERT: A Lite BERT for Self-supervised Learning of Language Representations`_
    by Zhenzhong Lan, Mingda Chen, Sebastian Goodman, Kevin Gimpel, Piyush Sharma, Radu Soricut. It presents
    two parameter-reduction techniques to lower memory consumption and increase the trainig speed of BERT.

    This model is a PyTorch `torch.nn.Module`_ sub-class. Use it as a regular PyTorch Module and
    refer to the PyTorch documentation for all matter related to general usage and behavior.

    .. _`ALBERT: A Lite BERT for Self-supervised Learning of Language Representations`:
        https://arxiv.org/abs/1909.11942

    .. _`torch.nn.Module`:
        https://pytorch.org/docs/stable/nn.html#module

    Parameters:
        config (:class:`~transformers.AlbertConfig`): Model configuration class with all the parameters of the model. 
            Initializing with a config file does not load the weights associated with the model, only the configuration.
            Check out the :meth:`~transformers.PreTrainedModel.from_pretrained` method to load the model weights.
"""

ALBERT_INPUTS_DOCSTRING = r"""
    Inputs:
        **input_ids**: ``torch.LongTensor`` of shape ``(batch_size, sequence_length)``:
            Indices of input sequence tokens in the vocabulary.
            To match pre-training, BERT input sequence should be formatted with [CLS] and [SEP] tokens as follows:

            (a) For sequence pairs:

                ``tokens:         [CLS] is this jack ##son ##ville ? [SEP] no it is not . [SEP]``
                
                ``token_type_ids:   0   0  0    0    0     0       0   0   1  1  1  1   1   1``

            (b) For single sequences:

                ``tokens:         [CLS] the dog is hairy . [SEP]``
                
                ``token_type_ids:   0   0   0   0  0     0   0``

            Albert is a model with absolute position embeddings so it's usually advised to pad the inputs on
            the right rather than the left.

            Indices can be obtained using :class:`transformers.AlbertTokenizer`.
            See :func:`transformers.PreTrainedTokenizer.encode` and
            :func:`transformers.PreTrainedTokenizer.convert_tokens_to_ids` for details.
        **attention_mask**: (`optional`) ``torch.FloatTensor`` of shape ``(batch_size, sequence_length)``:
            Mask to avoid performing attention on padding token indices.
            Mask values selected in ``[0, 1]``:
            ``1`` for tokens that are NOT MASKED, ``0`` for MASKED tokens.
        **token_type_ids**: (`optional`) ``torch.LongTensor`` of shape ``(batch_size, sequence_length)``:
            Segment token indices to indicate first and second portions of the inputs.
            Indices are selected in ``[0, 1]``: ``0`` corresponds to a `sentence A` token, ``1``
            corresponds to a `sentence B` token
            (see `BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding`_ for more details).
        **position_ids**: (`optional`) ``torch.LongTensor`` of shape ``(batch_size, sequence_length)``:
            Indices of positions of each input sequence tokens in the position embeddings.
            Selected in the range ``[0, config.max_position_embeddings - 1]``.
        **head_mask**: (`optional`) ``torch.FloatTensor`` of shape ``(num_heads,)`` or ``(num_layers, num_heads)``:
            Mask to nullify selected heads of the self-attention modules.
            Mask values selected in ``[0, 1]``:
            ``1`` indicates the head is **not masked**, ``0`` indicates the head is **masked**.
"""

@add_start_docstrings("The bare ALBERT Model transformer outputting raw hidden-states without any specific head on top.",
                      ALBERT_START_DOCSTRING, ALBERT_INPUTS_DOCSTRING)
class AlbertModel(AlbertPreTrainedModel):
    r"""
    Outputs: `Tuple` comprising various elements depending on the configuration (config) and inputs:
        **last_hidden_state**: ``torch.FloatTensor`` of shape ``(batch_size, sequence_length, hidden_size)``
            Sequence of hidden-states at the output of the last layer of the model.
        **pooler_output**: ``torch.FloatTensor`` of shape ``(batch_size, hidden_size)``
            Last layer hidden-state of the first token of the sequence (classification token)
            further processed by a Linear layer and a Tanh activation function. The Linear
            layer weights are trained from the next sentence prediction (classification)
            objective during Bert pretraining. This output is usually *not* a good summary
            of the semantic content of the input, you're often better with averaging or pooling
            the sequence of hidden-states for the whole input sequence.
        **hidden_states**: (`optional`, returned when ``config.output_hidden_states=True``)
            list of ``torch.FloatTensor`` (one for the output of each layer + the output of the embeddings)
            of shape ``(batch_size, sequence_length, hidden_size)``:
            Hidden-states of the model at the output of each layer plus the initial embedding outputs.
        **attentions**: (`optional`, returned when ``config.output_attentions=True``)
            list of ``torch.FloatTensor`` (one for each layer) of shape ``(batch_size, num_heads, sequence_length, sequence_length)``:
            Attentions weights after the attention softmax, used to compute the weighted average in the self-attention heads.
    """

    config_class = AlbertConfig
    pretrained_model_archive_map = ALBERT_PRETRAINED_MODEL_ARCHIVE_MAP
    load_tf_weights = load_tf_weights_in_albert
    base_model_prefix = "albert"

    def __init__(self, config):
        super(AlbertModel, self).__init__(config)

        self.config = config
        self.embeddings = AlbertEmbeddings(config)
        self.encoder = AlbertTransformer(config)
        self.pooler = nn.Linear(config.hidden_size, config.hidden_size)
        self.pooler_activation = nn.Tanh()

        self.init_weights()

    def get_input_embeddings(self):
        return self.embeddings.word_embeddings

    def set_input_embeddings(self, value):
        self.embeddings.word_embeddings = value

    def _resize_token_embeddings(self, new_num_tokens):
        old_embeddings = self.embeddings.word_embeddings
        new_embeddings = self._get_resized_embeddings(old_embeddings, new_num_tokens)
        self.embeddings.word_embeddings = new_embeddings
        return self.embeddings.word_embeddings

    def _prune_heads(self, heads_to_prune):
        """ Prunes heads of the model.
            heads_to_prune: dict of {layer_num: list of heads to prune in this layer}
            ALBERT has a different architecture in that its layers are shared across groups, which then has inner groups.
            If an ALBERT model has 12 hidden layers and 2 hidden groups, with two inner groups, there
            is a total of 4 different layers.

            These layers are flattened: the indices [0,1] correspond to the two inner groups of the first hidden layer,
            while [2,3] correspond to the two inner groups of the second hidden layer.

            Any layer with in index other than [0,1,2,3] will result in an error.
            See base class PreTrainedModel for more information about head pruning
        """
        for layer, heads in heads_to_prune.items():
            group_idx = int(layer / self.config.inner_group_num)
            inner_group_idx = int(layer - group_idx * self.config.inner_group_num)
            self.encoder.albert_layer_groups[group_idx].albert_layers[inner_group_idx].attention.prune_heads(heads)

    def forward(self, input_ids=None, attention_mask=None, token_type_ids=None, position_ids=None, head_mask=None,
                inputs_embeds=None):

        if input_ids is not None and inputs_embeds is not None:
            raise ValueError("You cannot specify both input_ids and inputs_embeds at the same time")
        elif input_ids is not None:
            input_shape = input_ids.size()
        elif inputs_embeds is not None:
            input_shape = inputs_embeds.size()[:-1]
        else:
            raise ValueError("You have to specify either input_ids or inputs_embeds")

        device = input_ids.device if input_ids is not None else inputs_embeds.device

        if attention_mask is None:
            attention_mask = torch.ones(input_shape, device=device)
        if token_type_ids is None:
            token_type_ids = torch.zeros(input_shape, dtype=torch.long, device=device)

        extended_attention_mask = attention_mask.unsqueeze(1).unsqueeze(2)
        extended_attention_mask = extended_attention_mask.to(dtype=next(self.parameters()).dtype) # fp16 compatibility
        extended_attention_mask = (1.0 - extended_attention_mask) * -10000.0
        if head_mask is not None:
            if head_mask.dim() == 1:
                head_mask = head_mask.unsqueeze(0).unsqueeze(0).unsqueeze(-1).unsqueeze(-1)
                head_mask = head_mask.expand(self.config.num_hidden_layers, -1, -1, -1, -1)
            elif head_mask.dim() == 2:
                head_mask = head_mask.unsqueeze(1).unsqueeze(-1).unsqueeze(-1)  # We can specify head_mask for each layer
            head_mask = head_mask.to(dtype=next(self.parameters()).dtype) # switch to fload if need + fp16 compatibility
        else:
            head_mask = [None] * self.config.num_hidden_layers

        embedding_output = self.embeddings(input_ids, position_ids=position_ids, token_type_ids=token_type_ids,
                                           inputs_embeds=inputs_embeds)
        encoder_outputs = self.encoder(embedding_output,
                                       extended_attention_mask,
                                       head_mask=head_mask)

        sequence_output = encoder_outputs[0]

        pooled_output = self.pooler_activation(self.pooler(sequence_output[:, 0]))

        outputs = (sequence_output, pooled_output) + encoder_outputs[1:]  # add hidden_states and attentions if they are here
        return outputs

class AlbertMLMHead(nn.Module):
    def __init__(self, config):
        super(AlbertMLMHead, self).__init__()

        self.LayerNorm = nn.LayerNorm(config.embedding_size)
        self.bias = nn.Parameter(torch.zeros(config.vocab_size))
        self.dense = nn.Linear(config.hidden_size, config.embedding_size)
        self.decoder = nn.Linear(config.embedding_size, config.vocab_size)
        self.activation = ACT2FN[config.hidden_act]

    def forward(self, hidden_states):
        hidden_states = self.dense(hidden_states)
        hidden_states = self.activation(hidden_states)
        hidden_states = self.LayerNorm(hidden_states)
        hidden_states = self.decoder(hidden_states)

        prediction_scores = hidden_states + self.bias

        return prediction_scores


@add_start_docstrings("Bert Model with a `language modeling` head on top.", ALBERT_START_DOCSTRING, ALBERT_INPUTS_DOCSTRING)
class AlbertForMaskedLM(AlbertPreTrainedModel):
    r"""
        **masked_lm_labels**: (`optional`) ``torch.LongTensor`` of shape ``(batch_size, sequence_length)``:
            Labels for computing the masked language modeling loss.
            Indices should be in ``[-1, 0, ..., config.vocab_size]`` (see ``input_ids`` docstring)
            Tokens with indices set to ``-1`` are ignored (masked), the loss is only computed for the tokens with labels
            in ``[0, ..., config.vocab_size]``

    Outputs: `Tuple` comprising various elements depending on the configuration (config) and inputs:
        **loss**: (`optional`, returned when ``masked_lm_labels`` is provided) ``torch.FloatTensor`` of shape ``(1,)``:
            Masked language modeling loss.
        **prediction_scores**: ``torch.FloatTensor`` of shape ``(batch_size, sequence_length, config.vocab_size)``
            Prediction scores of the language modeling head (scores for each vocabulary token before SoftMax).
        **hidden_states**: (`optional`, returned when ``config.output_hidden_states=True``)
            list of ``torch.FloatTensor`` (one for the output of each layer + the output of the embeddings)
            of shape ``(batch_size, sequence_length, hidden_size)``:
            Hidden-states of the model at the output of each layer plus the initial embedding outputs.
        **attentions**: (`optional`, returned when ``config.output_attentions=True``)
            list of ``torch.FloatTensor`` (one for each layer) of shape ``(batch_size, num_heads, sequence_length, sequence_length)``:
            Attentions weights after the attention softmax, used to compute the weighted average in the self-attention heads.
    """

    def __init__(self, config):
        super(AlbertForMaskedLM, self).__init__(config)

        self.albert = AlbertModel(config)
        self.predictions = AlbertMLMHead(config)

        self.init_weights()
        self.tie_weights()

    def tie_weights(self):
        """ Make sure we are sharing the input and output embeddings.
            Export to TorchScript can't handle parameter sharing so we are cloning them instead.
        """
        self._tie_or_clone_weights(self.predictions.decoder,
                                   self.albert.embeddings.word_embeddings)

    def get_output_embeddings(self):
        return self.predictions.decoder

    def forward(self, input_ids=None, attention_mask=None, token_type_ids=None, position_ids=None, head_mask=None, inputs_embeds=None,
                masked_lm_labels=None):
        outputs = self.albert(
            input_ids=input_ids,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds
        )
        sequence_outputs = outputs[0]

        prediction_scores = self.predictions(sequence_outputs)

        outputs = (prediction_scores,) + outputs[2:]  # Add hidden states and attention if they are here
        if masked_lm_labels is not None:
            loss_fct = CrossEntropyLoss(ignore_index=-1)
            masked_lm_loss = loss_fct(prediction_scores.view(-1, self.config.vocab_size), masked_lm_labels.view(-1))
            outputs = (masked_lm_loss,) + outputs

        return outputs


@add_start_docstrings("""Albert Model transformer with a sequence classification/regression head on top (a linear layer on top of
    the pooled output) e.g. for GLUE tasks. """,
    ALBERT_START_DOCSTRING, ALBERT_INPUTS_DOCSTRING)
class AlbertForSequenceClassification(AlbertPreTrainedModel):
    r"""
        **labels**: (`optional`) ``torch.LongTensor`` of shape ``(batch_size,)``:
            Labels for computing the sequence classification/regression loss.
            Indices should be in ``[0, ..., config.num_labels - 1]``.
            If ``config.num_labels == 1`` a regression loss is computed (Mean-Square loss),
            If ``config.num_labels > 1`` a classification loss is computed (Cross-Entropy).

    Outputs: `Tuple` comprising various elements depending on the configuration (config) and inputs:
        **loss**: (`optional`, returned when ``labels`` is provided) ``torch.FloatTensor`` of shape ``(1,)``:
            Classification (or regression if config.num_labels==1) loss.
        **logits**: ``torch.FloatTensor`` of shape ``(batch_size, config.num_labels)``
            Classification (or regression if config.num_labels==1) scores (before SoftMax).
        **hidden_states**: (`optional`, returned when ``config.output_hidden_states=True``)
            list of ``torch.FloatTensor`` (one for the output of each layer + the output of the embeddings)
            of shape ``(batch_size, sequence_length, hidden_size)``:
            Hidden-states of the model at the output of each layer plus the initial embedding outputs.
        **attentions**: (`optional`, returned when ``config.output_attentions=True``)
            list of ``torch.FloatTensor`` (one for each layer) of shape ``(batch_size, num_heads, sequence_length, sequence_length)``:
            Attentions weights after the attention softmax, used to compute the weighted average in the self-attention heads.

    Examples::

        tokenizer = AlbertTokenizer.from_pretrained('albert-base-v2')
        model = AlbertForSequenceClassification.from_pretrained('albert-base-v2')
        input_ids = torch.tensor(tokenizer.encode("Hello, my dog is cute")).unsqueeze(0)  # Batch size 1
        labels = torch.tensor([1]).unsqueeze(0)  # Batch size 1
        outputs = model(input_ids, labels=labels)
        loss, logits = outputs[:2]

    """
    def __init__(self, config):
        super(AlbertForSequenceClassification, self).__init__(config)
        self.num_labels = config.num_labels

        self.albert = AlbertModel(config)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.classifier = nn.Linear(config.hidden_size, self.config.num_labels)

        self.init_weights()

    def forward(self, input_ids=None, attention_mask=None, token_type_ids=None,
                position_ids=None, head_mask=None, inputs_embeds=None, labels=None):

        outputs = self.albert(
            input_ids=input_ids,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds
        )

        pooled_output = outputs[1]

        pooled_output = self.dropout(pooled_output)
        logits = self.classifier(pooled_output)

        outputs = (logits,) + outputs[2:]  # add hidden states and attention if they are here

        if labels is not None:
            if self.num_labels == 1:
                #  We are doing regression
                loss_fct = MSELoss()
                loss = loss_fct(logits.view(-1), labels.view(-1))
            else:
                loss_fct = CrossEntropyLoss()
                loss = loss_fct(logits.view(-1, self.num_labels), labels.view(-1))
            outputs = (loss,) + outputs

        return outputs  # (loss), logits, (hidden_states), (attentions)



@add_start_docstrings("""Albert Model with a span classification head on top for extractive question-answering tasks like SQuAD (a linear layers on top of
    the hidden-states output to compute `span start logits` and `span end logits`). """,
    ALBERT_START_DOCSTRING, ALBERT_INPUTS_DOCSTRING)
class AlbertForQuestionAnswering(AlbertPreTrainedModel):
    r"""
        **start_positions**: (`optional`) ``torch.LongTensor`` of shape ``(batch_size,)``:
            Labels for position (index) of the start of the labelled span for computing the token classification loss.
            Positions are clamped to the length of the sequence (`sequence_length`).
            Position outside of the sequence are not taken into account for computing the loss.
        **end_positions**: (`optional`) ``torch.LongTensor`` of shape ``(batch_size,)``:
            Labels for position (index) of the end of the labelled span for computing the token classification loss.
            Positions are clamped to the length of the sequence (`sequence_length`).
            Position outside of the sequence are not taken into account for computing the loss.

    Outputs: `Tuple` comprising various elements depending on the configuration (config) and inputs:
        **loss**: (`optional`, returned when ``labels`` is provided) ``torch.FloatTensor`` of shape ``(1,)``:
            Total span extraction loss is the sum of a Cross-Entropy for the start and end positions.
        **start_scores**: ``torch.FloatTensor`` of shape ``(batch_size, sequence_length,)``
            Span-start scores (before SoftMax).
        **end_scores**: ``torch.FloatTensor`` of shape ``(batch_size, sequence_length,)``
            Span-end scores (before SoftMax).
        **hidden_states**: (`optional`, returned when ``config.output_hidden_states=True``)
            list of ``torch.FloatTensor`` (one for the output of each layer + the output of the embeddings)
            of shape ``(batch_size, sequence_length, hidden_size)``:
            Hidden-states of the model at the output of each layer plus the initial embedding outputs.
        **attentions**: (`optional`, returned when ``config.output_attentions=True``)
            list of ``torch.FloatTensor`` (one for each layer) of shape ``(batch_size, num_heads, sequence_length, sequence_length)``:
            Attentions weights after the attention softmax, used to compute the weighted average in the self-attention heads.

    Examples::

        tokenizer = AlbertTokenizer.from_pretrained('albert-base-v2')
        model = AlbertForQuestionAnswering.from_pretrained('albert-base-v2')
        question, text = "Who was Jim Henson?", "Jim Henson was a nice puppet"
        input_text = "[CLS] " + question + " [SEP] " + text + " [SEP]"
        input_ids = tokenizer.encode(input_text)
        token_type_ids = [0 if i <= input_ids.index(102) else 1 for i in range(len(input_ids))] 
        start_scores, end_scores = model(torch.tensor([input_ids]), token_type_ids=torch.tensor([token_type_ids]))
        all_tokens = tokenizer.convert_ids_to_tokens(input_ids)  
        print(' '.join(all_tokens[torch.argmax(start_scores) : torch.argmax(end_scores)+1]))
        # a nice puppet


    """
    def __init__(self, config):
        super(AlbertForQuestionAnswering, self).__init__(config)
        self.num_labels = config.num_labels

        self.albert = AlbertModel(config)
        self.qa_outputs = nn.Linear(config.hidden_size, config.num_labels)

        self.init_weights()

    def forward(self, input_ids=None, attention_mask=None, token_type_ids=None, position_ids=None, head_mask=None,
                inputs_embeds=None, start_positions=None, end_positions=None):

        outputs = self.albert(
            input_ids=input_ids,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds
        )

        sequence_output = outputs[0]

        logits = self.qa_outputs(sequence_output)
        start_logits, end_logits = logits.split(1, dim=-1)
        start_logits = start_logits.squeeze(-1)
        end_logits = end_logits.squeeze(-1)

        outputs = (start_logits, end_logits,) + outputs[2:]
        if start_positions is not None and end_positions is not None:
            # If we are on multi-GPU, split add a dimension
            if len(start_positions.size()) > 1:
                start_positions = start_positions.squeeze(-1)
            if len(end_positions.size()) > 1:
                end_positions = end_positions.squeeze(-1)
            # sometimes the start/end positions are outside our model inputs, we ignore these terms
            ignored_index = start_logits.size(1)
            start_positions.clamp_(0, ignored_index)
            end_positions.clamp_(0, ignored_index)

            loss_fct = CrossEntropyLoss(ignore_index=ignored_index)
            start_loss = loss_fct(start_logits, start_positions)
            end_loss = loss_fct(end_logits, end_positions)
            total_loss = (start_loss + end_loss) / 2
            outputs = (total_loss,) + outputs

        return outputs  # (loss), start_logits, end_logits, (hidden_states), (attentions)

# separate two parts that need interaction such as Passage and Question
def split_context_query(sequence_output, pq_end_pos):
    context_max_len = sequence_output.size(1)
    query_max_len = sequence_output.size(1)
    sep_tok_len = 1 # [SEP]
    context_sequence_output = sequence_output.new(
        torch.Size((sequence_output.size(0), context_max_len, sequence_output.size(2)))).zero_()
    query_sequence_output = sequence_output.new_zeros(
        (sequence_output.size(0), query_max_len, sequence_output.size(2)))
    query_attention_mask = sequence_output.new_zeros((sequence_output.size(0), query_max_len))
    context_attention_mask = sequence_output.new_zeros((sequence_output.size(0), context_max_len))
    for i in range(0, sequence_output.size(0)):
        p_end = pq_end_pos[i][0]
        q_end = pq_end_pos[i][1]
        context_sequence_output[i, :min(context_max_len, p_end)] = sequence_output[i,
                                                                   1: 1 + min(context_max_len, p_end)]
        query_sequence_output[i, :min(query_max_len, q_end - p_end - sep_tok_len)] = sequence_output[i,
                                                                                     p_end + sep_tok_len + 1: p_end + sep_tok_len + 1 + min(
                                                                                         q_end - p_end - sep_tok_len,query_max_len)]
        query_attention_mask[i, :min(query_max_len, q_end - p_end - sep_tok_len)] = sequence_output.new_ones(
            (1, query_max_len))[0, :min(query_max_len, q_end - p_end - sep_tok_len)]
        context_attention_mask[i, : min(context_max_len, p_end)] = sequence_output.new_ones((1, context_max_len))[0,
                                                                   : min(context_max_len, p_end)]
    return context_sequence_output, query_sequence_output, context_attention_mask, query_attention_mask

class AlbertForMultipleChoice(AlbertPreTrainedModel):

    def __init__(self, config):
        super(AlbertForMultipleChoice, self).__init__(config)

        self.albert = AlbertModel(config)
        self.classifier = nn.Linear(config.hidden_size, 1)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)

        self.init_weights()

    def forward(self, input_ids=None, attention_mask=None, token_type_ids=None, position_ids=None, head_mask=None,
                inputs_embeds=None, labels=None):
        num_choices = input_ids.shape[1]

        flat_input_ids = input_ids.view(-1, input_ids.size(-1))
        flat_position_ids = position_ids.view(-1, position_ids.size(-1)) if position_ids is not None else None
        flat_token_type_ids = token_type_ids.view(-1, token_type_ids.size(-1)) if token_type_ids is not None else None
        flat_attention_mask = attention_mask.view(-1, attention_mask.size(-1)) if attention_mask is not None else None
        flat_head_mask= head_mask.view(-1, head_mask.size(-1)) if head_mask is not None else None
        flat_inputs_embeds= inputs_embeds.view(-1, inputs_embeds.size(-1)) if inputs_embeds is not None else None

        outputs = self.albert(
            input_ids=flat_input_ids,
            attention_mask=flat_attention_mask,
            token_type_ids=flat_token_type_ids,
            position_ids=flat_position_ids,
            head_mask=flat_head_mask,
            inputs_embeds=flat_inputs_embeds
        )

        sequence_output = outputs[0]
        pooled_output = torch.mean(sequence_output,1)
        pooled_output = self.dropout(pooled_output)
        logits=self.classifier(pooled_output)
        reshaped_logits = logits.view(-1, num_choices)

        outputs = (reshaped_logits,) + outputs[2:]  # add hidden states and attention if they are here

        if labels is not None:
            loss_fct = CrossEntropyLoss()
            loss = loss_fct(reshaped_logits, labels)
            outputs = (loss,) + outputs

        return outputs  # (loss), reshaped_logits, (hidden_states), (attentions)

class MultiHeadAttention(nn.Module):
    def __init__(self, config):
        super(MultiHeadAttention, self).__init__()
        if config.hidden_size % config.num_attention_heads != 0:
            raise ValueError(
                "The hidden size (%d) is not a multiple of the number of attention "
                "heads (%d)" % (config.hidden_size, config.num_attention_heads))
        self.output_attentions = config.output_attentions

        self.num_attention_heads = config.num_attention_heads
        self.attention_head_size = int(config.hidden_size / config.num_attention_heads)
        self.all_head_size = self.num_attention_heads * self.attention_head_size

        #W_i^Q
        self.query = nn.Linear(config.hidden_size, self.all_head_size)
        #W_i_K
        self.key = nn.Linear(config.hidden_size, self.all_head_size)
        #W_i_V
        self.value = nn.Linear(config.hidden_size, self.all_head_size)

        self.dropout = nn.Dropout(config.attention_probs_dropout_prob)

    #看起来是对于矩阵X求其X^T
    def transpose_for_scores(self, x):
        new_x_shape = x.size()[:-1] + (self.num_attention_heads, self.attention_head_size)
        x = x.view(*new_x_shape)
        # permute更改维度，1维和2维旋转
        return x.permute(0, 2, 1, 3)

    def forward(self, 
        context_states, 
        query_states, 
        attention_mask=None, 
        head_mask=None, 
        encoder_hidden_states=None, 
        encoder_attention_mask=None
    ):
        mixed_query_layer = self.query(query_states)

        extended_attention_mask = attention_mask[:, None, None, :]
        extended_attention_mask = extended_attention_mask.to(dtype=next(self.parameters()).dtype)  # fp16 compatibility
        extended_attention_mask = (1.0 - extended_attention_mask) * -10000.0
        attention_mask=extended_attention_mask

        # If this is instantiated as a cross-attention module, the keys
        # and values come from an encoder; the attention mask needs to be
        # such that the encoder's padding tokens are not attended to.
        if encoder_hidden_states is not None:
            mixed_key_layer = self.key(encoder_hidden_states)
            mixed_value_layer = self.value(encoder_hidden_states)
            attention_mask = encoder_attention_mask
        else:
            mixed_key_layer = self.key(context_states)
            mixed_value_layer = self.value(context_states)

        #先分别乘W_i^Q,W_i^K,W_i^V，然后过转置
        query_layer = self.transpose_for_scores(mixed_query_layer)
        key_layer = self.transpose_for_scores(mixed_key_layer)
        value_layer = self.transpose_for_scores(mixed_value_layer)

        # Take the dot product between "query" and "key" to get the raw attention scores.
        attention_scores = torch.matmul(query_layer, key_layer.transpose(-1, -2))
        #除以sqrt(d_k)
        attention_scores = attention_scores / math.sqrt(self.attention_head_size)
        if attention_mask is not None:
            # Apply the attention mask is (precomputed for all layers in BertModel forward() function)
            attention_scores = attention_scores + attention_mask

        #过softmax
        # Normalize the attention scores to probabilities.
        attention_probs = nn.Softmax(dim=-1)(attention_scores)

        # This is actually dropping out entire tokens to attend to, which might
        # seem a bit unusual, but is taken from the original Transformer paper.
        attention_probs = self.dropout(attention_probs)

        #可以乘上mask矩阵
        # Mask heads if we want to
        if head_mask is not None:
            attention_probs = attention_probs * head_mask

        #attention权重乘value
        context_layer = torch.matmul(attention_probs, value_layer)

        #转置后摊成一维的
        context_layer = context_layer.permute(0, 2, 1, 3).contiguous()
        new_context_layer_shape = context_layer.size()[:-2] + (self.all_head_size,)
        context_layer = context_layer.view(*new_context_layer_shape)

        # outputs = (context_layer, attention_probs) if self.output_attentions else (context_layer,)
        outputs = context_layer
        return outputs

class AlbertHRCA(AlbertPreTrainedModel):
    def __init__(self, config):
        super(AlbertPreTrainedModel, self).__init__(config)

        # Transformer Encoder
        self.albert = AlbertModel(config)
        #HRCA
        self.HRCA_layer= MultiHeadAttention(config)
        #dropout
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        #classifier
        self.classifier = nn.Linear(3*config.hidden_size, 1)

        self.init_weights()

    # 分割出P、Q、O对应tokens以及attention mask
    # [CLS] Passage [SEP] Question Option [SEP]
    def split_tokens(self,sequences,pqo_pos):
        #求出最长长度
        passage_max_len=sequences.size(1)
        question_max_len=sequences.size(1)
        option_max_len=sequences.size(1)
        sep_len=1
        #构建返回的token
        passage_seq_output=sequences.new_zeros(
            (sequences.size(0),passage_max_len,sequences.size(2))
        )
        question_seq_output=sequences.new_zeros(
            (sequences.size(0),question_max_len,sequences.size(2))
        )
        option_seq_output=sequences.new_zeros(
            (sequences.size(0),option_max_len,sequences.size(2))
        )
        #构建返回的attention mask
        passage_attention_mask=sequences.new_zeros(
            (sequences.size(0), passage_max_len)
        )
        question_attention_mask=sequences.new_zeros(
            (sequences.size(0), question_max_len)
        )
        option_attention_mask=sequences.new_zeros(
            (sequences.size(0), option_max_len)
        )
        #迭代
        for i in range(0,sequences.size(0)):
            #取出当前i对应的pqo的end
            p_end,q_end,o_end=pqo_pos[i][0],pqo_pos[i][1],pqo_pos[i][2]
            #print(p_end,q_end,o_end)
            #print(passage_seq_output.shape)
            #print(sequences.shape)
            #构建output
            p_start=1
            q_start=p_end+sep_len+1
            o_start=q_end+sep_len+1
            passage_seq_output[i,:min(passage_max_len,p_end)]=sequences[
                i,
                p_start:p_start+min(passage_max_len,p_end)
            ]
            question_seq_output[i,:min(question_max_len,q_end-p_end-sep_len)]=sequences[
                i,
                q_start:q_start+min(question_max_len,q_end-p_end-sep_len)
            ]
            option_seq_output[i,:min(option_max_len,o_end-q_end)]=sequences[
                i,
                o_start:o_start+min(option_max_len,o_end-q_end)
            ]
            #构建attention mask
            passage_attention_mask[i,:min(passage_max_len,p_end)]=sequences.new_ones((1,passage_max_len))[
                0,:min(passage_max_len,p_end)
            ]
            question_attention_mask[i,:min(question_max_len,q_end-p_end-sep_len)]=sequences.new_ones((1,question_max_len))[
                0,:min(question_max_len,q_end-p_end-sep_len)
            ]
            option_attention_mask[i,:min(option_max_len,o_end-q_end)]=sequences.new_ones((1,option_max_len))[
                0,:min(option_max_len,o_end-q_end)
            ]
        return passage_seq_output,question_seq_output,option_seq_output,passage_attention_mask,question_attention_mask,option_attention_mask



    def forward(self, 
        input_ids=None, 
        attention_mask=None, 
        token_type_ids=None, 
        position_ids=None, 
        head_mask=None,
        inputs_embeds=None, 
        labels=None, 
        pqo_pos=None, 
        iter=4
    ):
        num_choices = input_ids.shape[1]
        # 看起来是每个batch摊成1维的
        flat_input_ids = input_ids.view(-1, input_ids.size(-1))
        flat_position_ids = position_ids.view(-1, position_ids.size(-1)) if position_ids is not None else None
        flat_token_type_ids = token_type_ids.view(-1, token_type_ids.size(-1)) if token_type_ids is not None else None
        flat_attention_mask = attention_mask.view(-1, attention_mask.size(-1)) if attention_mask is not None else None
        flat_head_mask= head_mask.view(-1, head_mask.size(-1)) if head_mask is not None else None
        flat_inputs_embeds= inputs_embeds.view(-1, inputs_embeds.size(-1)) if inputs_embeds is not None else None

        # 过encoder
        outputs = self.albert(
            input_ids=flat_input_ids,
            attention_mask=flat_attention_mask,
            token_type_ids=flat_token_type_ids,
            position_ids=flat_position_ids,
            head_mask=flat_head_mask,
            inputs_embeds=flat_inputs_embeds
        )

        #取第一个输出（最后一层）
        sequences = outputs[0]

        #把pq pos摊成1维的
        pqo_pos = pqo_pos.view(-1, pqo_pos.size(-1))

        #利用pqo_pos把sequence分割成文章和Q+A两部分
        p_seq_out, q_seq_out, o_seq_out, p_att_mask,q_att_mask,o_att_mask = \
            self.split_tokens(sequences, pqo_pos)
        #迭代
        for _ in range(0,iter):
            #做co_attention
            # cq_biatt_output = self.bert_att(context_sequence_output, query_sequence_output, context_attention_mask)
            # qc_biatt_output = self.bert_att(query_sequence_output, context_sequence_output, query_attention_mask)
            q_seq_out=self.HRCA_layer(q_seq_out,q_seq_out,q_att_mask)
            o_seq_out=self.HRCA_layer(o_seq_out,q_seq_out,o_att_mask)
            p_seq_out=self.HRCA_layer(p_seq_out,o_seq_out,p_att_mask)
        # 合并，先对每列做平均值，之后竖着拼接。
        cat_output=torch.cat([torch.mean(q_seq_out,1), torch.mean(o_seq_out,1),torch.mean(p_seq_out,1) ], 1)
        #过dropout+Linear
        pooled_output=self.dropout(cat_output)
        logits=self.classifier(pooled_output)
        # 分类结果均摊用于计算
        reshaped_logits = logits.view(-1, num_choices)

        outputs = (reshaped_logits,) + outputs[2:]  # add hidden states and attention if they are here

        if labels is not None:
            loss_fct = CrossEntropyLoss()
            loss = loss_fct(reshaped_logits, labels)
            outputs = (loss,) + outputs

        return outputs  # (loss), reshaped_logits, (hidden_states), (attentions)

class AlbertHRCAPlus(AlbertPreTrainedModel):
    def __init__(self, config):
        super(AlbertPreTrainedModel, self).__init__(config)

        # Transformer Encoder
        self.albert = AlbertModel(config)
        #HRCA
        self.HRCA_layer= MultiHeadAttention(config)
        #dropout
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        #classifier
        self.classifier = nn.Linear(3*config.hidden_size, 1)

        self.init_weights()

    # 分割出P、Q、O对应tokens以及attention mask
    # [CLS] Passage [SEP] Question Option [SEP]
    def split_tokens(self,sequences,pqo_pos):
        #求出最长长度
        passage_max_len=sequences.size(1)
        question_max_len=sequences.size(1)
        option_max_len=sequences.size(1)
        sep_len=1
        #构建返回的token
        passage_seq_output=sequences.new_zeros(
            (sequences.size(0),passage_max_len,sequences.size(2))
        )
        question_seq_output=sequences.new_zeros(
            (sequences.size(0),question_max_len,sequences.size(2))
        )
        option_seq_output=sequences.new_zeros(
            (sequences.size(0),option_max_len,sequences.size(2))
        )
        #构建返回的attention mask
        passage_attention_mask=sequences.new_zeros(
            (sequences.size(0), passage_max_len)
        )
        question_attention_mask=sequences.new_zeros(
            (sequences.size(0), question_max_len)
        )
        option_attention_mask=sequences.new_zeros(
            (sequences.size(0), option_max_len)
        )
        #迭代
        for i in range(0,sequences.size(0)):
            #取出当前i对应的pqo的end
            p_end,q_end,o_end=pqo_pos[i][0],pqo_pos[i][1],pqo_pos[i][2]
            #print(p_end,q_end,o_end)
            #print(passage_seq_output.shape)
            #print(sequences.shape)
            #构建output
            p_start=1
            q_start=p_end+sep_len+1
            o_start=q_end+sep_len+1
            passage_seq_output[i,:min(passage_max_len,p_end)]=sequences[
                i,
                p_start:p_start+min(passage_max_len,p_end)
            ]
            question_seq_output[i,:min(question_max_len,q_end-p_end-sep_len)]=sequences[
                i,
                q_start:q_start+min(question_max_len,q_end-p_end-sep_len)
            ]
            option_seq_output[i,:min(option_max_len,o_end-q_end)]=sequences[
                i,
                o_start:o_start+min(option_max_len,o_end-q_end)
            ]
            #构建attention mask
            passage_attention_mask[i,:min(passage_max_len,p_end)]=sequences.new_ones((1,passage_max_len))[
                0,:min(passage_max_len,p_end)
            ]
            question_attention_mask[i,:min(question_max_len,q_end-p_end-sep_len)]=sequences.new_ones((1,question_max_len))[
                0,:min(question_max_len,q_end-p_end-sep_len)
            ]
            option_attention_mask[i,:min(option_max_len,o_end-q_end)]=sequences.new_ones((1,option_max_len))[
                0,:min(option_max_len,o_end-q_end)
            ]
        return passage_seq_output,question_seq_output,option_seq_output,passage_attention_mask,question_attention_mask,option_attention_mask



    def forward(self, 
        input_ids=None, 
        attention_mask=None, 
        token_type_ids=None, 
        position_ids=None, 
        head_mask=None,
        inputs_embeds=None, 
        labels=None, 
        pqo_pos=None, 
        iter=4
    ):
        num_choices = input_ids.shape[1]
        # 看起来是每个batch摊成1维的
        flat_input_ids = input_ids.view(-1, input_ids.size(-1))
        flat_position_ids = position_ids.view(-1, position_ids.size(-1)) if position_ids is not None else None
        flat_token_type_ids = token_type_ids.view(-1, token_type_ids.size(-1)) if token_type_ids is not None else None
        flat_attention_mask = attention_mask.view(-1, attention_mask.size(-1)) if attention_mask is not None else None
        flat_head_mask= head_mask.view(-1, head_mask.size(-1)) if head_mask is not None else None
        flat_inputs_embeds= inputs_embeds.view(-1, inputs_embeds.size(-1)) if inputs_embeds is not None else None

        # 过encoder
        outputs = self.albert(
            input_ids=flat_input_ids,
            attention_mask=flat_attention_mask,
            token_type_ids=flat_token_type_ids,
            position_ids=flat_position_ids,
            head_mask=flat_head_mask,
            inputs_embeds=flat_inputs_embeds
        )

        #取第一个输出（最后一层）
        sequences = outputs[0]

        #把pq pos摊成1维的
        pqo_pos = pqo_pos.view(-1, pqo_pos.size(-1))

        #利用pqo_pos把sequence分割成文章和Q+A两部分
        p_seq_out, q_seq_out, o_seq_out, p_att_mask,q_att_mask,o_att_mask = \
            self.split_tokens(sequences, pqo_pos)
        #迭代
        #与HRCA不一致的地方在于计算了9个关系
        #论文中有涉及到PQO Matrix，但同时计算顺序是固定的，因此这里不再实现PQO Matrix
        for _ in range(0,iter):
            #做co_attention
            # cq_biatt_output = self.bert_att(context_sequence_output, query_sequence_output, context_attention_mask)
            # qc_biatt_output = self.bert_att(query_sequence_output, context_sequence_output, query_attention_mask)
            q_seq_out=self.HRCA_layer(q_seq_out,q_seq_out,q_att_mask)   #1
            q_seq_out=self.HRCA_layer(q_seq_out,o_seq_out,q_att_mask)   #2
            o_seq_out=self.HRCA_layer(o_seq_out,o_seq_out,o_att_mask)   #3
            o_seq_out=self.HRCA_layer(o_seq_out,q_seq_out,o_att_mask)   #4
            o_seq_out=self.HRCA_layer(o_seq_out,p_seq_out,o_att_mask)   #5
            q_seq_out=self.HRCA_layer(q_seq_out,p_seq_out,q_att_mask)   #6
            p_seq_out=self.HRCA_layer(p_seq_out,p_seq_out,p_att_mask)   #7
            p_seq_out=self.HRCA_layer(p_seq_out,q_seq_out,p_att_mask)   #8
            p_seq_out=self.HRCA_layer(p_seq_out,o_seq_out,p_att_mask)   #9
        # 合并，先对每列做平均值，之后竖着拼接。
        cat_output=torch.cat([torch.mean(q_seq_out,1), torch.mean(o_seq_out,1),torch.mean(p_seq_out,1) ], 1)
        #过dropout+Linear
        pooled_output=self.dropout(cat_output)
        logits=self.classifier(pooled_output)
        # 分类结果均摊用于计算
        reshaped_logits = logits.view(-1, num_choices)

        outputs = (reshaped_logits,) + outputs[2:]  # add hidden states and attention if they are here

        if labels is not None:
            loss_fct = CrossEntropyLoss()
            loss = loss_fct(reshaped_logits, labels)
            outputs = (loss,) + outputs

        return outputs  # (loss), reshaped_logits, (hidden_states), (attentions)
