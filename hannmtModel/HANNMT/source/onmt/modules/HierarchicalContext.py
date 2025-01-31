### Copyright (c) 2018 Idiap Research Institute, http://www.idiap.ch/
### Written by Lesly Miculicich <lmiculicich@idiap.ch>
#
# This file is part of HAN-NMT.
#
# HAN-NMT is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3 as
# published by the Free Software Foundation.
#
# HAN-NMT is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with HAN-NMT. If not, see <http://www.gnu.org/licenses/>.

import torch, sys
import torch.nn as nn
from torch.autograd import Variable
import numpy as np
import copy

import onmt
from onmt.Models import EncoderBase
from onmt.Models import DecoderState
from onmt.Utils import aeq
from onmt.modules.Embeddings import PositionalEncoding
from onmt.modules.Attention import Attention
from onmt.modules import PositionwiseFeedForward
from onmt.modules import FeedForward

class HierarchicalContext(nn.Module):

	def __init__(self, size, dropout,
				 head_count=8, hidden_size=2048,
				 context_type=None, context_size=3,
				 padding_idx=1, tok_idx=[]):
		super(HierarchicalContext, self).__init__()

		self.context_type = context_type
		self.context_size = context_size
		self.padding_idx = padding_idx
		self.tok_idx = tok_idx

		self.layer_norm_query_word = onmt.modules.LayerNorm(size)
		self.layer_norm_query_sent = onmt.modules.LayerNorm(size)
		self.layer_norm_word = onmt.modules.LayerNorm(size)
		self.dropout = nn.Dropout(dropout)
		self.sent_attn = onmt.modules.MultiHeadedAttention(head_count, size, dropout=dropout)
		self.word_attn = onmt.modules.MultiHeadedAttention(head_count, size, dropout=dropout)

		self.linear = nn.Linear(2*size, size)
		self.sigmoid = nn.Sigmoid()

		self.layer_norm_sent = onmt.modules.LayerNorm(size)

		#if 'poswise_ff' in self.context_type:
		self.feed_forward = PositionwiseFeedForward(size,
												hidden_size,
												dropout)
		#	self.feed_forward_s = PositionwiseFeedForward(size,
		#											hidden_size,
		#											dropout)
		#else:
		#	self.feed_forward = FeedForward(size, dropout)
		#	self.feed_forward_s = FeedForward(size, dropout)


	def _get_word_context(self, query, context, index, mask_word):

		b_size, t_size, d_size = query.size()
		b_size_, s_size, d_size_ = context.size()
		aeq(d_size, d_size_)
		b_size__, c_size = index.size()
		aeq(b_size, b_size__)
		b_size__, t_size_, s_size_ = mask_word.size()
		aeq(b_size_, b_size__)
		aeq(s_size, s_size_)
		aeq(t_size, t_size_)

		# Create padding mask for previous sentences
		mask_sent = index < 0
		index_ = copy.deepcopy(index)
		index_[mask_sent] = 0

		# Get context
		context_ = context.view(b_size_, -1).expand(b_size, b_size_, s_size*d_size)
		index__ = index_.unsqueeze(2).expand(b_size , c_size, s_size*d_size)
		context_word = torch.gather(context_, 1, Variable(index__,
					requires_grad=False)).view(b_size*c_size, s_size, d_size)

		# Get mask for context
		mask_ = mask_word.contiguous().view(b_size_, -1).expand(b_size, b_size_, t_size_*s_size).cuda()
		index__ = index_.unsqueeze(2).expand(b_size, c_size, t_size_*s_size).cuda()
		context_pad_mask = torch.gather(mask_, 1, index__).view(b_size*c_size, t_size_, s_size)

		# Mask previous sentences
		mask_sent_ = mask_sent.unsqueeze(2).expand(b_size,
					c_size, t_size_*s_size).contiguous().view(b_size*c_size, t_size_, s_size)
		context_pad_mask[mask_sent_] = self.padding_idx

		# Expand query for each context sentence
		query_ = query.unsqueeze(1).expand(b_size , c_size,
					t_size, d_size).contiguous().view(b_size*c_size, t_size, d_size)

		return query_, context_word, context_pad_mask,

	def _get_sent_context(self, query, context_word, context_index, attn_word):

		b_size, t_size, d_size = query.size()
		_, c_size = context_index.size()

		# Sequence size now context_wordis context size
		context_sent = context_word.view(b_size, c_size, t_size, d_size).transpose(1,2).contiguous().view(b_size*t_size, c_size, d_size)

		# Creating the mask for padding by word and sentence
		mask_sent = context_index < 0
		context_pad_mask = mask_sent.unsqueeze(1).expand(b_size,
					t_size, c_size).contiguous().view(b_size*t_size, -1)
		context_pad_mask = context_pad_mask.unsqueeze(1).contiguous()

		# Re-arrange the query
		query_ = query.view(b_size*t_size, 1, d_size)

		_,h,t,s = attn_word.size()
		aeq(t, t_size)
		attn_word = attn_word.view(b_size, c_size, h, t, s)

		return query_, context_sent, context_pad_mask, attn_word

	def get_context_index(self, context_index, b_size, b_size_, batch_i):

		if batch_i is None:
			index = np.matrix([range(b_size)])-1
			index = index.repeat(self.context_size, axis=0)
			mask = np.zeros([self.context_size, self.context_size], dtype=bool)

			for i in range(self.context_size):
				index[i,:] -= self.context_size-1-i
				mask[i,0:self.context_size-i] = True

			for i in context_index:
				index[:,i:min(i+self.context_size, b_size)][mask[:,:min(self.context_size, b_size - i)]] = -1
			index = np.transpose(index)
		else:
			if batch_i == 0:
				index = np.zeros([b_size, self.context_size])-1
			else:
				assert b_size_ <= self.context_size, "Error in context"
				index = np.matrix(range(b_size_-self.context_size, 0) + range(b_size_))
				index = index.repeat(b_size, axis=0)

		return index

	def forward(self, input, query, context, context_index, cache=None, batch_i=None):

		input = input[:, :, 0].transpose(0,1).contiguous()
		query = query.transpose(0,1).contiguous()
		context = context.transpose(0,1).contiguous()

		in_batch, in_len = input.size()
		in_pad_mask = input.data.eq(self.padding_idx)
		for k in self.tok_idx:
			in_pad_mask = in_pad_mask|input.data.eq(k)

		in_pad_mask = in_pad_mask.unsqueeze(1).expand(in_batch, query.size()[1], in_len)

		b_size, t_size, d_size = query.size()
		b_size_, k_size, d_size = context.size()

		query_ = Variable(query.data)
		context = Variable(context.data)

		query_word_norm = self.layer_norm_query_word(query_)
		query_sent_norm = self.layer_norm_query_sent(query_)

		index = torch.Tensor(self.get_context_index(context_index, b_size, b_size_, batch_i)).type_as(query.data).long()

		# Re-arrange the tensors for matching words
		query_word_norm, context_word, context_word_mask = self._get_word_context(query_word_norm, context, index.cuda(), in_pad_mask)

		# Attention over words
		context_word, attn_word = self.word_attn(context_word, context_word,
										query_word_norm, mask=context_word_mask, all_attn=True)
		# Norm layer
		context_sent = self.layer_norm_word(context_word)

		# Re-arrange the tensors for matching sentences
		query_sent_norm, context_sent, context_sent_mask, attn_word = self._get_sent_context(query_sent_norm,
																context_sent, index, attn_word)

		# Attention over sentences
		sent_context, attn_sent = self.sent_attn(context_sent, context_sent, query_sent_norm,
									  mask=context_sent_mask, all_attn=True)

		sent_context = sent_context.view(b_size, t_size, d_size)
		_, h, _, c = attn_sent.size()
		attn_sent = attn_sent.view(b_size, t_size, h, 1, c)
		sent_context = self.feed_forward(sent_context)

		l = self.sigmoid(self.linear(torch.cat([query, sent_context], dim=2)))
		out = (1-l)*query + l*sent_context

		out = self.layer_norm_sent(out)

		return out.transpose(0,1).contiguous(), attn_word, attn_sent
