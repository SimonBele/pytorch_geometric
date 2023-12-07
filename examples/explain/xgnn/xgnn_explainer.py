import os.path as osp
import os

import torch
import torch.nn.functional as F
from torch_geometric.data import Data, Batch
from torch_geometric.explain import Explainer, XGNNExplainer
from torch_geometric.nn import GCNConv
from torch.nn import BatchNorm1d
from torch_geometric.nn import global_mean_pool
from torch_geometric.datasets import TUDataset


import random
# # TODO: Get our acyclic dataset
# dataset = 'Cora'
# path = osp.join(osp.dirname(osp.realpath(__file__)), '..', 'data', 'Planetoid')
# dataset = Planetoid(path, dataset)
# data = dataset[0]
from xgnn_model import GCN_Graph

device = 'cuda' if torch.cuda.is_available() else 'cpu'

args = {'device': device,
        'dropout': 0.1,
        'epochs': 1000,
        'input_dim' : 7,
        'opt': 'adam',
        'opt_scheduler': 'none',
        'opt_restart': 0,
        'weight_decay': 5e-5,
        'lr': 0.001}
class objectview(object):
    def __init__(self, d):
        self.__dict__ = d
        
args = objectview(args)
        
model = GCN_Graph(args.input_dim, output_dim=2, dropout=args.dropout).to(device)

# Assume 'model_to_freeze' is the model you want to freeze
for param in model.parameters():
    param.requires_grad = False
    
# depending on os change path
path = "examples/explain/xgnn/mutag_model.pth"
if os.name == 'nt':
    path = "examples\\explain\\xgnn\\mutag_model.pth"

model.load_state_dict(torch.load(path, map_location=torch.device('cpu')))
model.to(device)

def create_single_batch(dataset):
    data_list = [data for data in dataset]
    batched_data = Batch.from_data_list(data_list)
    return batched_data


def custom_softmax(arr, axis=0):
    non_zero_indices = torch.where(arr != 0)
    arr_non_zero = arr[non_zero_indices]
    arr_non_zero = F.softmax(arr_non_zero, dim=axis)
    arr[non_zero_indices] = arr_non_zero
    return arr


def check_edge_representation(data):
    # Convert edge indices to a set of tuples for easier comparison
    edge_set = {tuple(edge) for edge in data.edge_index.t().tolist()}

    for edge in edge_set:
        reverse_edge = (edge[1], edge[0])
        if reverse_edge not in edge_set:
            return "Edges are represented as single, undirected edges"
    return "Edges are represented with two directed edges, one in each direction"



class GraphGenerator(torch.nn.Module):
    def __init__(self, num_node_features, num_candidate_node_types):
        super(GraphGenerator, self).__init__()
        # TODO: Check 
        print("debug: GraphGenerator init")
        print("num_node_features", num_node_features)
        print("num_candidate_node_types", num_candidate_node_types)
        print("debug: GraphGenerator init done")
        self.gcn_layers = torch.nn.ModuleList([
            GCNConv(num_node_features, 16),
            GCNConv(16, 24),
            GCNConv(24, 32)
        ])

        self.mlp_start_node = torch.nn.Sequential(
            torch.nn.Linear(32, 16),
            torch.nn.ReLU6(),
            torch.nn.Linear(16, 1),
            torch.nn.Softmax(dim=0)
        )
        self.mlp_end_node = torch.nn.Sequential(
            torch.nn.Linear(32, 24),
            torch.nn.ReLU6(),
            torch.nn.Linear(24, 1),
            torch.nn.Softmax(dim=0)
        )

    def forward(self, graph_state, candidate_set):
        # contatenate graph_state features with candidate_set features
        node_features_graph = graph_state.x
        candidate_features = torch.stack(list(candidate_set.values()))
        node_features = torch.cat((node_features_graph, candidate_features), dim=0).float()
        
        # run through GCN layers
        for gcn_layer in self.gcn_layers:
            node_features = gcn_layer(node_features, graph_state.edge_index)
        # get start node probabilities and mask out candidates
        start_node_probs = self.mlp_start_node(node_features)
        
        candidate_set_mask = torch.ones_like(start_node_probs)
        candidate_set_indices = torch.arange(node_features_graph.shape[0], node_features.shape[0])
        # set to minimum possible value
        candidate_set_mask[candidate_set_indices] = 0
        start_node_probs = start_node_probs * candidate_set_mask
        # change 0 probabilities to very small number
        #start_node_probs[start_node_probs == 0] = 1e-10
        start_node_probs = start_node_probs.squeeze()
        start_node_probs = custom_softmax(start_node_probs)

        # sample start node
        p_start = torch.distributions.Categorical(start_node_probs)
        start_node = p_start.sample()
        
        # get end node probabilities and mask out start node
        # combined_features = torch.cat((node_features, node_features[start_node].unsqueeze(0)), dim=0)
        end_node_probs = self.mlp_end_node(node_features)
        start_node_mask = torch.ones_like(end_node_probs)
        start_node_mask[start_node] = 0
        end_node_probs = end_node_probs * start_node_mask
        # change 0 probabilities to very small number
        #end_node_probs[end_node_probs == 0] = 1e-10
        end_node_probs = end_node_probs.squeeze()
        end_node_probs = custom_softmax(end_node_probs)
        
        # sample end node
        end_node = torch.distributions.Categorical(end_node_probs).sample()
        if end_node >= graph_state.x.shape[0]: 
            # add new node features to graph state
            graph_state.x = torch.cat((graph_state.x, 
                                       list(candidate_set.values())[end_node - graph_state.x.shape[0]].unsqueeze(0)), dim=0).float() # TODO: make prettier
            graph_state.node_type += [list(candidate_set.keys())[end_node - graph_state.x.shape[0]],] # TODO: make prettier
            new_edge = torch.tensor([[start_node], [graph_state.x.shape[0] - 1]])
        else: 
            new_edge = torch.tensor([[start_node], [end_node]])
        graph_state.edge_index = torch.cat((graph_state.edge_index, new_edge), dim=1)
        
        # one hot encoding of start and end node
        start_node_one_hot = torch.zeros_like(start_node_probs)
        start_node_one_hot[start_node] = 1
        end_node_one_hot = torch.zeros_like(end_node_probs)
        end_node_one_hot[end_node] = 1

        return ((start_node_probs, start_node_one_hot), (end_node_probs, end_node_one_hot)), graph_state

class RLGenExplainer(XGNNExplainer):
    def __init__(self, candidate_set, validity_args):
        super(RLGenExplainer, self).__init__()
        self.candidate_set = candidate_set
        self.graph_generator = GraphGenerator(len(list(self.candidate_set.values())[0]),
                                              len(self.candidate_set)) # TODO: get number of features out in a prettier way
        self.max_steps = 10
        self.lambda_1 = 1
        self.lambda_2 = 1
        self.num_classes = 2
        self.validity_args = validity_args
    
    def reward_tf(self, pre_trained_gnn, graph_state, target_class, num_classes):
        graph_state_batch = create_single_batch([graph_state,])
        
        # Move graph batch to the same device as your model
        graph_state_batch = graph_state_batch.to(device)

        gnn_output = pre_trained_gnn(graph_state_batch)
        probability_of_target_class = gnn_output[0][target_class]
        return probability_of_target_class - 1 / num_classes
    
    def rollout_reward(self, intermediate_graph_state, pre_trained_gnn, target_class, num_classes, num_rollouts=5):
        final_rewards = []
        for _ in range(num_rollouts):
            # Generate a final graph from the intermediate graph state
            _, final_graph = self.graph_generator(intermediate_graph_state, self.candidate_set)
            # Evaluate the final graph
            reward = self.reward_tf(pre_trained_gnn, final_graph, target_class, num_classes)
            final_rewards.append(reward)
        # Average the rewards from all rollouts
        average_final_reward = sum(final_rewards) / len(final_rewards)
        return average_final_reward

    # def evaluate_graph_validity(self, graph_state):
    #     # check if graph has duplicated edges
    #     edge_set = set()
        
    #     for edge in graph_state.edge_index:
    #         sorted_edge = tuple(sorted(edge))
    #         if sorted_edge in edge_set:
    #             print("Graph has duplicated edges")
    #             return -1
    #         edge_set.add(sorted_edge)
    #     return 0

    def evaluate_graph_validity(self, graph_state):
        # For mutag, node degrees cannot exceed valency
        degree = torch.zeros(graph_state.num_nodes, dtype=torch.long)
        for (u, v) in zip(graph_state.edge_index[0], graph_state.edge_index[1]):
            degree[u] += 1
            degree[v] += 1 # TODO: please fix this it is uglier than pepe the frog
          
        for node_idx in range(graph_state.num_nodes):
            if degree[node_idx] > self.validity_args[graph_state.node_type[node_idx]]:
                return -1

        return 0
        
    def calculate_reward(self, graph_state, pre_trained_gnn, target_class, num_classes):
        intermediate_reward = self.reward_tf(pre_trained_gnn, graph_state, target_class, num_classes)
        # Assuming rollout function is defined to perform graph rollouts and evaluate
        final_graph_reward = self.rollout_reward(graph_state, pre_trained_gnn, target_class, num_classes)
        # Compute graph validity score (R_tr)
        # defined based on the specific graph rules of the dataset
        graph_validity_score = self.evaluate_graph_validity(graph_state) 
        reward = intermediate_reward + self.lambda_1 * final_graph_reward + self.lambda_2 * graph_validity_score
        return reward

    # Training function
    def train_generative_model(self, model_to_explain, for_class, num_epochs, learning_rate):
        optimizer = torch.optim.Adam(self.graph_generator.parameters(), lr=learning_rate)
        for epoch in range(num_epochs):
            total_loss = 0


            # we sample from node IDs and candidate keys set which is now a dictionary, then create a data object with the correct node type and edge index
            random_node_type = random.choice(list(candidate_set.keys()))
            feature = candidate_set[random_node_type].unsqueeze(0)
            edge_index = torch.tensor([], dtype=torch.long).view(2, -1)
            node_type = [random_node_type,]
            initial_graph = Data(x=feature, edge_index=edge_index, node_type=node_type)
            current_graph_state = initial_graph
            # sample from candidate set and create initial graph state
            # random_index = torch.randint(0, self.candidate_set.size(0), (1,))
            # sampled_node = self.candidate_set[random_index] # sample a node from the candidate set
            # edge_index = torch.tensor([], dtype=torch.long).view(2, -1)
            # initial_graph = Data(x=sampled_node, edge_index=edge_index)
            # current_graph_state = initial_graph
            
            for step in range(self.max_steps):
                ((p_start, a_start), (p_end, a_end)), new_graph_state = self.graph_generator(current_graph_state, self.candidate_set)
                
                reward = self.calculate_reward(new_graph_state, model_to_explain, for_class, self.num_classes)
                
                LCE_start = F.cross_entropy(p_start, a_start)
                LCE_end = F.cross_entropy(p_end, a_end)
                
                loss = -reward * (LCE_start + LCE_end)
                total_loss += -reward * (LCE_start.item() + LCE_end.item())
                
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
                
                if reward >= 0:
                    current_graph_state = new_graph_state
            print(f"Epoch {epoch} completed, Total Loss: {total_loss}")
            
        return self.graph_generator 


device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
#model = GCN().to(device)
#data = data.to(device)
optimizer = torch.optim.Adam(model.parameters(), lr=0.01, weight_decay=5e-4)

# extract features for the candidate set
dataset = TUDataset(root='/tmp/MUTAG', name='MUTAG')
all_features = torch.cat([data.x for data in dataset], dim=0)

# we hope these are in the same order
max_valency = {'C': 4, 'N': 5, 'O': 2, 'F': 1, 'I': 7, 'Cl': 7, 'Br': 5}

# node type map that maps node type to a one hot vector encoding torch tensor please
candidate_set = {'C': torch.tensor([1, 0, 0, 0, 0, 0, 0]),
                 'N': torch.tensor([0, 1, 0, 0, 0, 0, 0]),
                 'O': torch.tensor([0, 0, 1, 0, 0, 0, 0]),
                 'F': torch.tensor([0, 0, 0, 1, 0, 0, 0]),
                 'I': torch.tensor([0, 0, 0, 0, 1, 0, 0]),
                 'Cl': torch.tensor([0, 0, 0, 0, 0, 1, 0]),
                 'Br': torch.tensor([0, 0, 0, 0, 0, 0, 1])}

# validity_args_valency = {candidate_set[atom] : max_val for atom, max_val in max_valency.items()}

explainer = Explainer(
    model = model,
    algorithm = RLGenExplainer(candidate_set=candidate_set, validity_args = max_valency),
    explanation_type = 'generative',
    node_mask_type = None,
    edge_mask_type = None,
    model_config = dict(
        mode = 'multiclass_classification',
        task_level = 'node',
        return_type = 'log_probs',
    ),
)

print("EXPLAINER DONE!")

class_index = 1
target = torch.tensor([0, 1])

explanation = explainer(None, None, target=target) # Generates explanations for all classes at once
print(explanation)

