from turtle import forward
from torchrec.model.module.layers import MLPModule
from torchrec.model import basemodel, loss_func, scorer
from torchrec.data import dataset
from torchrec.ann import sampler 
from torch import nn
import torch.nn.functional as F
import torch
import collections
import numpy as np
import random
from torchrec.model.module import aggregator

class LabelAggregator(nn.Module):
    def forward(self, self_vectors, neighbors_agg, reset_masks):
        neighbors_agg = neighbors_agg.squeeze(dim=-1)
        output = reset_masks.float() * self_vectors + torch.logical_not(reset_masks).float() * neighbors_agg
        return output

class KGNNLSConv(nn.Module):
    def __init__(self, n_iter, dim, n_neighbor, dropout, alg_type):
        super().__init__()
        self.n_iter = n_iter
        self.dim = dim
        self.n_neighbor = n_neighbor
        self.dropout = dropout
        if alg_type == 'sum':
            self.aggregator_class = aggregator.GCNAggregator
        elif alg_type == 'concat':
            self.aggregator_class = aggregator.GraphSageAggregator
        elif alg_type == 'neighbor':
            self.aggregator_class = aggregator.NeighborAggregator
        self.alg_type = alg_type
        self.labelAggregator = LabelAggregator()
        self.aggregators = torch.nn.ModuleList()
        for i in range(self.n_iter):
            if i == self.n_iter - 1:
                self.aggregators.append(self.aggregator_class(self.dim, self.dim, dropout=self.dropout, act=torch.nn.Tanh()))
            else:
                self.aggregators.append(self.aggregator_class(self.dim, self.dim, dropout=self.dropout))
        
    def _mix_neighbor_vectors(self, neighbor_vectors, neighbor_relations, user_embeddings):
        """
        Args: 
            neighbor_vectors(torch.Tensor): shape: (batch_size, n_neighbor^(i-1), n_neighbor, dim) or (batch_size, n_neighbor^(i-1), n_neighbor, 1)
            embeddings or labels of the neighbors in i-th layers. 
            neighbor_relations(torch.Tensor): shape: (batch_size, n_neighbor^(i-1), n_neighbor, dim). embeddings of the relations in i-th layers. 
            user_embeddings(torch.Tensor): shape: (batch_size, embed_dim). The embeddings of the query users used to calculate the weights of relations.
        Returns:
            neighbors_aggregated(torch.Tensor): shape: (batch_size, n_neighbor^(i-1), dim). The aggregation of neighbors and each neighbor has a different weight.
        """
        # [batch_size, 1, 1, dim]
        user_embeddings = user_embeddings.reshape(-1, 1, 1, self.dim)
        # [batch_size, -1, n_neighbor, dim] -> [batch_size, -1, n_neighbor]
        user_relation_score = torch.mean(neighbor_relations * user_embeddings, dim=-1)
        # [batch_size, -1, n_neighbor] -> [batch_size, -1, n_neighbor, 1]
        user_ralation_score_normalized = torch.softmax(user_relation_score, dim=-1).unsqueeze(-1)
        # [batch_size, -1, n_neighbor, dim] -> [batch_size, -1, dim] 
        neighbors_aggregated = torch.mean(user_ralation_score_normalized * neighbor_vectors, dim=-2)
        return neighbors_aggregated
    
    def forward(self, entity_vectors, relation_vectors, user_embeddings, reset_masks=None):
        for i in range(self.n_iter):
            entity_vectors_next_iter = []
            for hop in range(self.n_iter - i):
                shape = [entity_vectors[hop].size(0), -1, self.n_neighbor, self.dim]
                if reset_masks == None:
                    neighbors_agg = self._mix_neighbor_vectors(
                        entity_vectors[hop + 1].reshape(shape), 
                        relation_vectors[hop].reshape(shape), 
                        user_embeddings)
                    vector = self.aggregators[i](entity_vectors[hop], neighbors_agg)
                else:
                    neighbors_agg = self._mix_neighbor_vectors(
                        entity_vectors[hop + 1].reshape([entity_vectors[hop].size(0), -1, self.n_neighbor, 1]), 
                        relation_vectors[hop].reshape(shape), 
                        user_embeddings)
                    vector = self.labelAggregator(entity_vectors[hop], neighbors_agg, reset_masks[hop])
                entity_vectors_next_iter.append(vector)
            entity_vectors = entity_vectors_next_iter
        if reset_masks == None:
            return entity_vectors[0].reshape(-1, self.dim)
        else:
            return entity_vectors[0].squeeze(dim=-1)

class KGNNLSItemEncoder(nn.Module):
    def __init__(self, ent_emb, rel_emb, config):
        super().__init__()
        self.ent_emb = ent_emb
        self.rel_emb = rel_emb
        self.KGNNLSConv = KGNNLSConv(config['n_iter'], config['embed_dim'], config['neighbor_sample_size'], \
             0.0, config['aggregator_type'])

    def forward(self, entities, relations, user_embeddings):
        # entity_vector: [batch_size, -1, dim]
        entity_vectors = [self.ent_emb(i) for i in entities]
        relation_vectors = [self.rel_emb(i) for i in relations]
        item_embeddings = self.KGNNLSConv(entity_vectors, relation_vectors, user_embeddings)
        return item_embeddings
"""
KGNNLS
###############
    Knowledge-aware Graph Neural Networks with Label Smoothness Regularization for Recommender Systems(KDD'19)
    Reference:
        https://doi.org/10.1145/3292500.3330836
"""

class KGNNLS(basemodel.TwoTowerRecommender):
    """
    KGNNLS is built upon KGCN. To alleviate the over-fit problem in KGCN, a regularization on edge weights is added into KGNNLS based on label smoothness assumption. 
    """
    def __init__(self, config):
        self.kg_index = config['kg_network_index']
        self.n_iter = config['n_iter']
        self.neighbor_sample_size = config['neighbor_sample_size']
        self.n_neighbor = self.neighbor_sample_size
        self.aggregator_type = config['aggregator_type']
        self.ls_weight = config['ls_weight']
        super().__init__(config)

    def init_model(self, train_data):
        self.fhid = train_data.get_network_field(self.kg_index, 0, 0)
        self.ftid = train_data.get_network_field(self.kg_index, 0, 1)
        self.frid = train_data.get_network_field(self.kg_index, 0, 2)
        self.num_entities = train_data.num_values(self.fhid)
        self.num_items = train_data.num_items
        self.kg = self._construct_kg(train_data.network_feat[self.kg_index])
        self.adj_entity, self.adj_relation = self._construct_adj()
        self.ent_emb = basemodel.Embedding(train_data.num_values(self.fhid), self.embed_dim, padding_idx=0)
        self.rel_emb = nn.Embedding(train_data.num_values(self.frid), self.embed_dim, padding_idx=0)   
        super().init_model(train_data)
        # LS regularization
        self.interaction_table, self.offset = self.get_interaction_table(train_data)  
         
    def get_dataset_class(self):
        return dataset.MFDataset

    def config_scorer(self):
        return scorer.InnerProductScorer()

    def config_loss(self):
        return nn.BCEWithLogitsLoss()

    def build_item_encoder(self, train_data):
        return KGNNLSItemEncoder(self.ent_emb, self.rel_emb, self.config)

    def build_user_encoder(self, train_data):
        return torch.nn.Embedding(train_data.num_users, self.embed_dim, padding_idx=0)

    def construct_query(self, batch_data):
        return self.user_encoder(batch_data[self.fuid])

    def _construct_kg(self, kg_feat):
        """
        Construct knowledge graph dict. 

        Args:
            kg_feat(TensorFrame): the triplets in knowledge graph.

        Returns:
            kg(defaultidict): the key is ``head_id``, and the value is a list. The list contains all triplets whose heads are ``head_id``.
        """
        # head -> [(tail, relation), (tail, relation), ..., (tail, relation)]
        kg = collections.defaultdict(list)
        for i in range(len(kg_feat)):
            row = kg_feat[i]
            head_id = row[self.fhid].item()
            tail_id = row[self.ftid].item()
            relation_id = row[self.frid].item()
            kg[head_id].append((tail_id, relation_id))
            kg[tail_id].append((head_id, relation_id)) # treat the KG as an undirected graph
        return kg

    def _construct_adj(self):
        # each line of adj_entity stores the sampled neighbor entities for a given entity
        # each line of adj_relation stores the corresponding sampled neighbor relations
        adj_entity = np.zeros([self.num_entities, self.neighbor_sample_size], dtype=np.int64)
        adj_relation = np.zeros([self.num_entities, self.neighbor_sample_size], dtype=np.int64)
        for entity in range(self.num_entities):
            neighbors = self.kg[entity]
            if neighbors == []: # padding id has no neighbors
                continue
            n_neighbors = len(neighbors)
            if n_neighbors >= self.neighbor_sample_size:
                sampled_indices = np.random.choice(list(range(n_neighbors)), size=self.neighbor_sample_size, replace=False)
            else:
                sampled_indices = np.random.choice(list(range(n_neighbors)), size=self.neighbor_sample_size, replace=True)
            adj_entity[entity] = np.array([neighbors[i][0] for i in sampled_indices])
            adj_relation[entity] = np.array([neighbors[i][1] for i in sampled_indices])

        return torch.from_numpy(adj_entity), torch.from_numpy(adj_relation)

    def _get_neighbors(self, seeds):
        self.adj_entity = self.adj_entity.to(self.device)
        self.adj_relation = self.adj_relation.to(self.device)
        seeds = seeds.unsqueeze(-1)
        entities = [seeds]
        relations = []
        for i in range(self.n_iter):
            neighbor_entities = self.adj_entity[entities[i]].reshape(seeds.size(0), -1)
            neighbor_relations = self.adj_relation[entities[i]].reshape(seeds.size(0), -1)
            entities.append(neighbor_entities)
            relations.append(neighbor_relations)
        return entities, relations

    def get_interaction_table(self, train_data):
        '''
        Gets interaction table of user-item pairs. Every user-item pair is tranformed into a integer. 
        Some unobserved pairs will be sampled as negative samples. 
        The value of a positive pair is 1.0 and the value of a negative pair is 0.0. 
        
        Args:
            train_data(MFDataset): to get inter_feat
        Returns:
            interaction_table(dict): key: an integer representing a user-item pair. value: 1.0 or 0.0.
            offset(int): the offset used in the tranformation.  
        '''
        inter_feat = train_data.inter_feat
        users = inter_feat.get_col(self.fuid)[train_data.inter_feat_subset].int()
        items = inter_feat.get_col(self.fiid)[train_data.inter_feat_subset].int()
        offset = len(str(self.num_entities))
        offset = 10 ** offset 
        keys = (users * offset + items).int().tolist()
        values = [1.] * len(users)
        interaction_table = dict(zip(keys, values))
        # negative sample
        pos_num = len(interaction_table)
        neg_num = 0
        while neg_num < pos_num:
            user_id = random.randint(1, train_data.num_users)
            item_id = random.randint(1, train_data.num_items)
            key = user_id * offset + item_id
            if key not in interaction_table:
                interaction_table[key] = 0.
                neg_num += 1
        
        return interaction_table, offset

    def calculate_label_smoothness_loss(self, users, user_embeddings, entities, relations):
        '''
        Calculates LS regularization.
        
        Args:
            users(torch.Tensor): shape: (batch_size)
            user_embeddings(torch.Tensor): shape: (batch_size, dim)
            embeddings of the users.
            entities(list): the multi-hop neighbors of the items.
            relations(list): the multi-hop relations of the items.
        Returns: 
            labels(torch.Tensor): shape: (batch_size, 1)
            the predicted labels based on label smoothness assumption.
        '''
        entity_labels = []
        reset_masks = []
        holdout_item_for_user = None 
        # [batch_size, 1]
        users = users.unsqueeze(dim=-1)
        for entities_per_iter in entities:
            # [batch_size, n_neighbor^i]
            user_entity_concat = users * self.offset + entities_per_iter
            # the first one in entities is the items to be held out
            if holdout_item_for_user is None:
                holdout_item_for_user = user_entity_concat

            def look_up_interaction_table(a, b):
                return self.interaction_table.setdefault(b, 0.5)
            # [batch_size, n_neighbor^i]
            initial_label = user_entity_concat.cpu().double()
            initial_label.map_(initial_label, look_up_interaction_table)
            initial_label = initial_label.to(self.device)
            # False if the item is held out
            holdout_mask = (holdout_item_for_user - user_entity_concat).bool() 
            # True if the entity is a labeled item
            reset_mask = (initial_label - 0.5).bool()
            # remove held-out items
            reset_mask = torch.logical_and(reset_mask, holdout_mask)
            initial_label = holdout_mask.float() * initial_label \
                + torch.logical_not(holdout_mask).float() * 0.5  # label initialization
            reset_masks.append(reset_mask)
            entity_labels.append(initial_label)
        reset_masks = reset_masks[:-1]  # we do not need the reset_mask for the last iteration
        
        # label propagation
        relation_vectors = [self.rel_emb(i) for i in relations]
        return self.item_encoder.KGNNLSConv(entity_labels, relation_vectors, user_embeddings, reset_masks)

    def forward(self, batch_data):
        pos_query = self.construct_query(batch_data) # [batch_size, dim]
        pos_items = self.get_item_feat(batch_data)
        # get negative items, neg_item_idx : [batch_size, neg]
        pos_prob, neg_item_idx, neg_prob = self.sampler(pos_query, self.neg_count, pos_items)
        neg_item_idx = neg_item_idx.flatten()
        neg_user_idx = batch_data[self.fuid].repeat_interleave(self.neg_count)
        batch_data[self.fuid] = torch.cat([batch_data[self.fuid], neg_user_idx])
        batch_data[self.fiid] = torch.cat([batch_data[self.fiid], neg_item_idx])

        query = self.construct_query(batch_data)
        # {[batch_size, 1], [batch_size, n_neighbor], [batch_size, n_neighbor^2], ..., [batch_size, n_neighbor^n_iter]}
        entities, relations = self._get_neighbors(batch_data[self.fiid])
        item_embeddings = self.item_encoder(entities, relations, query)
        y_h = self.score_func(query, item_embeddings)

        y = torch.zeros(query.size(0)).to(self.device)
        y[ : len(pos_query)] = 1

        # calculate LS regularization.
        ls_loss = self.calculate_label_smoothness_loss(batch_data[self.fuid], query, entities, relations)
        return y, y_h, ls_loss

    def training_step(self, batch, batch_idx):
        y, y_h, ls_loss = self.forward(batch)
        loss = self.loss_fn(y_h, y) + self.ls_weight * ls_loss
        return loss

    def get_item_vector(self):
        return None

    def on_train_epoch_start(self):
        pass

    def prepare_testing(self):
        pass   

    def _test_step(self, batch, metric, cutoffs):
        items = torch.arange(1, self.num_items).to(self.device)
        items = items.tile(batch[self.fuid].size(0))
        users = batch[self.fuid].repeat_interleave(self.num_items - 1)
        user_embeddings = self.user_encoder(users)
        entities, relations = self._get_neighbors(items)
        self.item_vector = self.item_encoder(entities, relations, user_embeddings).reshape(-1, self.num_items - 1, self.embed_dim)
        return super()._test_step(batch, metric, cutoffs)

# TODO It is hard to get item_vector on train epoch start, because item_vector depends on user_embeddings
