import os
import sys
import datetime
import networkx as nx
import numpy as np
from builtins import ImportError

from graph_partitioning import utils

#import graph_partitioning.metrics.dct_metrics as nmi_metrics
from sklearn.metrics.cluster import normalized_mutual_info_score

class NoPartitionerException(Exception):
    """
    Raised when no partitioner has been specified.
    """

class GraphPartitioning:

    UNMAPPED = -1
    _quiet = False

    def __init__(self, *config, **kwargs):
        for dictionary in config:
            for key in dictionary:
                setattr(self, key, dictionary[key])
        for key in kwargs:
            setattr(self, key, kwargs[key])

        if(self.verbose == 0):
            self._quiet = True

        self.originalG = None
        self._batches = []

        self.compute_output_filenames()

    def __enter__(self):
        return self

    def __exit__(self, *err):
        pass

    def __del__(self):
        self.lonelyNodeModifier = None

    def load_network(self):
        self.lonelyNodeModifier = utils.LonelyNodesModifier(self, 0.2, 'after', isEnabled=True)

        # read METIS file
        self.G = utils.read_metis(self.DATA_FILENAME)

        # Inject here additional lonely nodes
        #num_lonely = utils.add_lonely_nodes_to_graph(self.G, 0.2)
        #num_lonely = 0

        self.initial_number_of_nodes = self.G.number_of_nodes() # used for computing metrics

        # Alpha value used in prediction model
        self.prediction_model_alpha = self.G.number_of_edges() * (self.num_partitions / self.G.number_of_nodes()**2)

        if self.use_one_shot_alpha:
            self.prediction_model_alpha = self.one_shot_alpha

        # Order of nodes arriving
        self.arrival_order = list(range(0, self.G.number_of_nodes()))

        if self.SIMULATED_ARRIVAL_FILE == "":
            # mark all nodes as needing a shelter
            self.simulated_arrival_list = [1]*self.G.number_of_nodes()
        else:
            with open(self.SIMULATED_ARRIVAL_FILE, "r") as ar:
                self.simulated_arrival_list = [int(line.rstrip('\n')) for line in ar]

        #if num_lonely > 0:
        #    arrivals = []
        #    for i in range(0, num_lonely):
        #        arrivals.append(1)
        #    before = True
        #    if before:
        #        self.simulated_arrival_list = arrivals + self.simulated_arrival_list
        #    else:
        #        self.simulated_arrival_list = self.simulated_arrival_list + arrivals

        self.lonelyNodeModifier.generateLonelyNodes()
        self.lonelyNodeModifier.setLonelyCoordinates()
        #print(self.lonelyNodeModifier.coordPath)

        # count the number of people arriving in the simulation
        self.number_simulated_arrivals = 0
        for arrival in self.simulated_arrival_list:
            self.number_simulated_arrivals += arrival

        if self.verbose > 0:
            print("Graph loaded...")
            print(nx.info(self.G))
            if nx.is_directed(self.G):
                print("Graph is directed")
            else:
                print("Graph is undirected")

        self.reset()

        # load displacement prediction weights
        if(self.PREDICTION_LIST_FILE == ""):
            self.predicted_displacement_weights = [1] * self.G.number_of_nodes()
        else:
            with open(self.PREDICTION_LIST_FILE, 'r') as plf:
                self.predicted_displacement_weights = [float(line.rstrip('\n')) for line in plf]

        # preserve original node/edge weight when modification functions are applied
        if self.graph_modification_functions:
            node_weights = {n[0]: n[1]['weight'] for n in self.G.nodes_iter(data=True)}
            nx.set_node_attributes(self.G, 'weight_orig', node_weights)

            edge_weights = {(e[0], e[1]): e[2]['weight'] for e in self.G.edges_iter(data=True)}
            nx.set_edge_attributes(self.G, 'weight_orig', edge_weights)

        # make a copy of the original graph since we may alter the graph later on
        # and may want to refer to information from the original graph
        self.originalG = self.G.copy()

    def init_partitioner(self):
        self.prediction_model_algorithm = None
        self.partition_algorithm = None

        if self.PREDICTION_MODEL_ALGORITHM == 'FENNEL' or self.PARTITIONER_ALGORITHM == 'FENNEL':

            import pyximport
            pyximport.install()
            from graph_partitioning import fennel

            if self.PREDICTION_MODEL_ALGORITHM == 'FENNEL':
                self.prediction_model_algorithm = fennel.FennelPartitioner(self.prediction_model_alpha, self.FENNEL_FRIEND_OF_A_FRIEND_ENABLED)
                if self.verbose > 0:
                    print("FENNEL partitioner loaded for generating PREDICTION MODEL.")

            if self.PARTITIONER_ALGORITHM == 'FENNEL':
                self.partition_algorithm = fennel.FennelPartitioner(FRIEND_OF_FRIEND_ENABLED=self.FENNEL_FRIEND_OF_A_FRIEND_ENABLED)
                if self.verbose > 0:
                    print("FENNEL partitioner loaded for making shelter assignments.")

            if self.FENNEL_NODE_REORDERING_ENABLED:
                if self.FENNEL_NODE_REODERING_SCHEME == 'PII_LH':
                    # compute the node leverage centrality scores for the whole graph
                    self.fennel_centrality_reordered_nodes = utils.piiNodeOrder(self.G, self.G.nodes())
                elif self.FENNEL_NODE_REODERING_SCHEME == 'LEVERAGE_HL':
                    # compute the node leverage centrality scores for the whole graph
                    self.fennel_centrality_reordered_nodes = utils.leverage_centrality(self.G, lonely_at_end=False)
                elif self.FENNEL_NODE_REODERING_SCHEME == 'DEGREE_HL':
                    self.fennel_centrality_reordered_nodes = utils.degree_centrality(self.G)
                #elif self.FENNEL_NODE_REORDERING_SCHEME == 'BOTTLENECK_HL':
                # for bottleneck, we must reorder just one batch at a time, not the whole network, because it is extremely slow!
                #    self.fennel_centrality_reordered_nodes = utils.bottleneck_node_ordering(self.G, self.G.nodes())


        if self.PREDICTION_MODEL_ALGORITHM == 'SCOTCH':

            #sys.path.insert(0, self.SCOTCH_PYLIB_REL_PATH)

            # check if the library is present
            if not os.path.isfile(self.SCOTCH_LIB_PATH):
                raise ImportError("Could not locate the SCOTCH library file at: {}".format(self.SCOTCH_LIB_PATH))

            from graph_partitioning import scotch_partitioner

            self.prediction_model_algorithm = scotch_partitioner.ScotchPartitioner(self.SCOTCH_LIB_PATH, virtualNodesEnabled=self.use_virtual_nodes)
            if self.verbose > 0:
                print("SCOTCH partitioner loaded for generating PREDICTION MODEL.")

            if self.PARTITIONER_ALGORITHM == 'SCOTCH':
                # use the same prediction_model_algorithm for both batch and prediction modes
                self.partition_algorithm = self.prediction_model_algorithm
                if self.verbose > 0:
                    print("SCOTCH partitioner loaded for making shelter assignments.")

        if self.PREDICTION_MODEL_ALGORITHM == 'PATOH':
            #sys.path.insert(0, self.SCOTCH_PYLIB_REL_PATH)


            # check if the library is present
            if not os.path.isfile(self.PATOH_LIB_PATH):
                raise ImportError("Could not locate the PaToH library file at: {}".format(self.PATOH_LIB_PATH))

            from graph_partitioning import patoh_partitioner

            isQuiet = False
            if(self.verbose == 0):
                isQuiet = True

            self.prediction_model_algorithm = patoh_partitioner.PatohPartitioner(self.PATOH_LIB_PATH, quiet=isQuiet,
                partitioningIterations=self.PATOH_ITERATIONS, hyperedgeExpansionMode=self.PATOH_HYPEREDGE_EXPANSION_MODE)
            if self.verbose > 0:
                print("PaToH partitioner loaded for generating PREDICTION MODEL.")

            if self.PARTITIONER_ALGORITHM == 'PATOH':
                # use the same prediction_model_algorithm for both batch and prediction modes
                self.partition_algorithm = self.prediction_model_algorithm
                if self.verbose > 0:
                    print("PaToH partitioner loaded for making shelter assignments.")

            # edge expansion happens via hyperedge expansion in PaToH's patoh_data.py script
            if(self.edge_expansion_enabled):
                self.edge_expansion_enabled = False
                self.prediction_model_algorithm.hyperedgeExpansionMode = self.PATOH_HYPEREDGE_EXPANSION_MODE
            else:
                self.prediction_model_algorithm.hyperedgeExpansionMode = 'no_expansion'



        if self.prediction_model_algorithm == None:
            raise NoPartitionerException("Prediction model partitioner not specified or incorrect. Available partitioners are 'FENNEL' or 'SCOTCH'.")
        if self.partition_algorithm == None:
            raise NoPartitionerException("Assignment partitioner not specified or incorrect. Available partitioners is 'FENNEL'.")

    def reset(self):

        self.assignments = np.repeat(np.int32(self.UNMAPPED), self.G.number_of_nodes())
        self.assignments_prediction_model = np.repeat(np.int32(self.UNMAPPED), self.G.number_of_nodes())
        self.fixed = np.repeat(np.int32(self.UNMAPPED), self.G.number_of_nodes())
        self.nodes_arrived = []
        self.virtual_nodes = []
        self.virtual_edges = []


    def prediction_model(self):
        if self.PREDICTION_MODEL_ALGORITHM == 'SCOTCH':
            # update SCOTCH Strategy to 'quality' partition weights for prediction model
            self.partition_algorithm.partitionStrategy = 'quality'

        if self.apply_prediction_model_weights:
            self.apply_graph_prediction_weights()

        if self.PREDICTION_MODEL:
            with open(self.PREDICTION_MODEL, "r") as inf:
                self.assignments = np.fromiter(inf.readlines(), dtype=np.int32)
                self.assignments_prediction_model = np.array(self.assignments, copy=True)
        else:
            if self.graph_modification_functions:
                self.G = self._edge_expansion(self.G)
            self.assignments = self.prediction_model_algorithm.generate_prediction_model(self.G, self.num_iterations, self.num_partitions, self.assignments, self.fixed)
            self.assignments_prediction_model = np.array(self.assignments, copy=True)

        if self.verbose > 0:
            print("PREDICTION MODEL")
            print("----------------\n")

        run_metrics = [self._print_score()]
        self._print_assignments()

        if self.verbose > 0:
            nodes_fixed = len([o for o in self.fixed if o == 1])
            print("\nFixed: {}".format(nodes_fixed))

            utils.print_partitions(self.G, self.assignments, self.num_partitions)

        if self.use_virtual_nodes:
            self.init_virtual_nodes()

        if self.apply_prediction_model_weights:
            self.remove_graph_prediction_weights()

        if self.PREDICTION_MODEL_ALGORITHM == 'FENNEL':
            # store a copy of the original graph in the fennel partitioner
            self.partition_algorithm.original_graph = self.G

        return run_metrics

    def init_virtual_nodes(self):
        print("Creating virtual nodes and assigning edges based on prediction model")

        # create virtual nodes
        self.virtual_nodes = list(range(self.G.number_of_nodes(), self.G.number_of_nodes() + self.num_partitions))
        print("\nVirtual nodes:")

        # create virtual edges
        self.virtual_edges = []
        for n in range(0, self.G.number_of_nodes()):
            self.virtual_edges += [(n, self.virtual_nodes[self.assignments[n]])]

        # extend assignments
        self.assignments = np.append(self.assignments, np.array(list(range(0, self.num_partitions)), dtype=np.int32))
        self.fixed = np.append(self.fixed, np.array([1] * self.num_partitions, dtype=np.int32))

        self.G.add_nodes_from(self.virtual_nodes, weight=1.0)
        self.G.add_edges_from(self.virtual_edges, weight=self.virtual_edge_weight)

        if self.verbose > 0:
            self._print_assignments()
            print("Last {} nodes are virtual nodes.".format(self.num_partitions))


    def _print_assignments(self):
        if self.verbose > 0:
            print("\nAssignments:")
            utils.fixed_width_print(self.assignments)

    def _print_score(self, graph=None):
        if self.compute_metrics_enabled == False:
            return [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]

        if graph == None:
            graph = self.G

        # waste, cut_ratio
        x = utils.score(graph, self.assignments, self.num_partitions)

        edges_cut, steps, cut_edges = utils.base_metrics(graph, self.assignments)

        #q_qds_conductance = utils.louvainModularityComQuality(graph, self.assignments, self.num_partitions)
        # non-overlapping metrics
        q_qds_conductance = utils.infomapModularityComQuality(graph, self.assignments, self.num_partitions)

        loneliness = utils.loneliness_score_wavg(graph, self.loneliness_score_param, self.assignments, self.num_partitions)
        #print('loneliness', loneliness)
        #max_perm = utils.run_max_perm(graph)
        max_perm = utils.wavg_max_perm(graph, self.assignments, self.num_partitions)

        rbse_list = utils.ratherBeSomewhereElseList(graph, self.assignments, self.num_partitions)
        rbse = utils.ratherBeSomewhereElseMetric(rbse_list)

        #nmi_score = nmi_metrics.nmi(np.array([self.assignments_prediction_model, self.assignments]))
        nmi_assignments = self.assignments.tolist()
        pred_assignments = self.assignments_prediction_model.tolist()
        pred_nmi_assignments = []
        actual_nmi_assignments = []
        if self.use_virtual_nodes:
            #print(len(nmi_assignments), self.initial_number_of_nodes)
            if(len(nmi_assignments) > self.initial_number_of_nodes):
                nmi_assignments = nmi_assignments[0: self.initial_number_of_nodes]

        for i, partition in enumerate(nmi_assignments):
            if partition >= 0:
                pred_nmi_assignments.append(pred_assignments[i])
                actual_nmi_assignments.append(partition)

        #print('computing nmi', len(pred_nmi_assignments), len(actual_nmi_assignments))

        nmi_score = normalized_mutual_info_score(pred_nmi_assignments, actual_nmi_assignments)
        #nmi_score = 0.0

        # compute the sum of edge weights for all the cut edges for a total score
        #total_cut_weight = 0
        #for cutEdge in cut_edges:
        #    total_cut_weight += self.originalG.edge[cutEdge[0]][cutEdge[1]]['weight']

        # compute fscores
        #print('computing fscore...')
        fscore, fscore_relabelled = utils.fscores2(self.assignments_prediction_model, self.assignments, self.num_partitions)
        #fscore = 0.0
        #fscore_relabelled = 0.0
        #print('fscores:', fscore, fscore_relabelled)

        if self.verbose > 1:
            print("{0:.5f}\t\t{1:.10f}\t{2}\t\t{3}\t\t\t{4:.5f}\t{5:.5f}\t{6}\t{7:.5f}\t{8:.10f}\t{9}\t{10}\t{11:.5f}".format(x[0], x[1], edges_cut, steps, q_qds_conductance[1], q_qds_conductance[2], max_perm, rbse, nmi_score, fscore, abs(fscore-fscore_relabelled), loneliness))
            #print("{0:.5f}\t\t{1:.10f}\t{2}\t\t{3}\t\t\t{4:.5f}\t{5:.5f}\t{6:.5f}\t{7}\t{8}\t{9:.10f}\t{10}\t{11}\t{12}".format(x[0], x[1], edges_cut, steps, q_qds_conductance[0], q_qds_conductance[1], q_qds_conductance[2], loneliness, max_perm, nmi_score, total_cut_weight, fscore, abs(fscore-fscore_relabelled)))
            #print("{0:.5f}\t\t{1:.10f}\t{2}\t\t{3}\t\t\t{4}\t{5}\t{6}\t{7:.10f}\t{8}\t{9}\t{10}".format(x[0], x[1], edges_cut, steps, mod, loneliness, max_perm, nmi_score, total_cut_weight, fscore, abs(fscore-fscore_relabelled)))

        return [x[0], x[1], edges_cut, steps, q_qds_conductance[1], q_qds_conductance[2], max_perm, rbse, nmi_score, fscore, abs(fscore-fscore_relabelled), loneliness]
        #return [x[0], x[1], edges_cut, steps, q_qds_conductance[0], q_qds_conductance[1], q_qds_conductance[2], loneliness, max_perm, nmi_score, total_cut_weight, fscore, abs(fscore-fscore_relabelled)]
        #return [x[0], x[1], edges_cut, steps, mod, loneliness, max_perm, nmi_score, total_cut_weight, fscore, abs(fscore-fscore_relabelled)]

    def assign_cut_off(self):
        # stop assignments when x% of arriving people is assinged
        cut_off_value = int(self.prediction_model_cut_off * self.number_simulated_arrivals)
        # @deprecated cut_off_value when x% of whole population arrives
        #cut_off_value = int(self.prediction_model_cut_off * self.G.number_of_nodes())
        if self.verbose > 0:
            if self.prediction_model_cut_off == 0:
                print("Discarding prediction model\n")
            else:
                print("Assign first {} arrivals using prediction model, then discard\n".format(cut_off_value))

        # fix arrivals
        for a in self.arrival_order:
            # check if node needs a shelter
            if self.simulated_arrival_list[a] == 0:
                continue

            # set 100% node weight for those that need a shelter
            if self.graph_modification_functions and self.alter_arrived_node_weight_to_100:
                self.G.node[a]['weight'] = 100

            nodes_fixed = len([o for o in self.fixed if o == 1])
            if nodes_fixed >= cut_off_value:
                break
            self.fixed[a] = 1
            self.nodes_arrived.append(a)

        # remove nodes not fixed, ie. discard prediction model
        for i in range(0, len(self.assignments)):
            if self.fixed[i] == -1:
                self.assignments[i] = -1

        GSub = self.G.subgraph(self.nodes_arrived)

        run_metrics = [self._print_score(GSub)]
        self._print_assignments()

        if self.verbose > 0:
            nodes_fixed = len([o for o in self.fixed if o == 1])
            print("\nFixed: {}".format(nodes_fixed))

            utils.print_partitions(self.G, self.assignments, self.num_partitions)

        return run_metrics


    def _edge_expansion(self, G):
        if(self.edge_expansion_enabled == False):
            return G

        # Update edge weights for nodes that have an assigned probability of displacement
        for edge in self.G.edges_iter(data=True):
            left = edge[0]
            right = edge[1]

            edge_weight = 1.0
            try:
                edge_weight = edge[2]['weight_orig']
            except Exception as err:
                # this may due to the fact that virtual nodes have no original weight
                pass

            w1 = float(G.node[left]['weight'])
            w2 = float(G.node[right]['weight'])

            # edge expansion
            if self.EDGE_EXPANSION_MODE == 'minimum':
                if w1 < w2:
                    edge[2]['weight'] = w1
                else:
                    edge[2]['weight'] = w2
            elif self.EDGE_EXPANSION_MODE == 'maximum':
                if w1 < w2:
                    edge[2]['weight'] = w2
                else:
                    edge[2]['weight'] = w1
            elif self.EDGE_EXPANSION_MODE == 'product':
                edge[2]['weight'] = w1 * w2
            elif self.EDGE_EXPANSION_MODE == 'product_squared':
                edge[2]['weight'] = (w1 * w2) ** 2
            elif self.EDGE_EXPANSION_MODE == 'sqrt_product':
                edge[2]['weight'] = (w1 * w2) ** 0.5
            elif self.EDGE_EXPANSION_MODE == 'average':
                edge[2]['weight'] = (w1 + w2) * 0.5
            elif self.EDGE_EXPANSION_MODE == 'total':
                edge[2]['weight'] = w1 + w2
            else:
                # new edge weight
                edge[2]['weight'] = (float(G.node[left]['weight']) * edge_weight) * (float(G.node[right]['weight']) * edge_weight)

            #print('edge_weight', self.EDGE_EXPANSION_MODE, w1, w2, edge[2]['weight'])

            if left in self.nodes_arrived or right in self.nodes_arrived:
                # change the emphasis of the prediction model
                edge[2]['weight'] = edge[2]['weight'] * self.prediction_model_emphasis

        return G


    def batch_arrival(self):
        if self.verbose > 0:
            print("Assigning in batches of {}".format(self.restream_batches))
            print("--------------------------------\n")

        if self.PARTITIONER_ALGORITHM == 'SCOTCH':
            # update SCOTCH Strategy to 'balance' partition weights, rather than  default 'quality' strategy
            self.partition_algorithm.partitionStrategy = 'balance'

        batch_arrived = []
        run_metrics = []
        for i, a in enumerate(self.arrival_order):

            # check if node is already arrived
            if self.fixed[a] == 1:
                continue

            # GRAPH MODIFICATION FUNCTIONS
            if self.graph_modification_functions:

                # remove nodes that don't need a shelter
                if self.simulated_arrival_list[a] == 0:
                    self.G.remove_node(a)
                    continue

                # set 100% node weight for those that need a shelter
                if self.alter_arrived_node_weight_to_100:
                    self.G.node[a]['weight'] = 100

            # skip nodes that don't need a shelter
            if self.simulated_arrival_list[a] == 0:
                continue

            batch_arrived.append(a)

            # batch processing
            if self.restream_batches == len(batch_arrived):
                run_metrics += self.process_batch(batch_arrived)
                if not self.sliding_window:
                    batch_arrived = []

        # process remaining nodes in incomplete batch
        run_metrics += self.process_batch(batch_arrived, assign_all=True)

        # remove nodes not fixed
        for i in range(0, len(self.assignments)):
            if self.fixed[i] == -1:
                self.assignments[i] = -1

        if self.verbose > 0:
            self._print_assignments()

            nodes_fixed = len([o for o in self.fixed if o == 1])
            print("\nFixed: {}".format(nodes_fixed))

            utils.print_partitions(self.G, self.assignments, self.num_partitions)

        return run_metrics


    def process_batch(self, batch_arrived, assign_all=False):
        # re-order batch_arrived using a FENNEL re-ordering algorithm, if required and configured to do so
        # TODO add configuration for this
        if self.PARTITIONER_ALGORITHM == 'FENNEL':
            if self.FENNEL_NODE_REORDERING_ENABLED:
                if self.FENNEL_NODE_REODERING_SCHEME == 'PII_LH':
                    reordered_batch = utils.reorder_nodes_based_on_pii_centrality(self.fennel_centrality_reordered_nodes, batch_arrived)
                elif self.FENNEL_NODE_REODERING_SCHEME == 'LEVERAGE_HL':
                    reordered_batch = utils.reorder_nodes_based_on_leverage_centrality(self.fennel_centrality_reordered_nodes, batch_arrived)
                elif self.FENNEL_NODE_REODERING_SCHEME == 'DEGREE_HL':
                    reordered_batch = utils.reorder_nodes_based_on_degree_centrality(self.fennel_centrality_reordered_nodes, batch_arrived)
                elif self.FENNEL_NODE_REODERING_SCHEME == 'BOTTLENECK_HL':
                    # for bottleneck, we must reorder just this batch, because it is extremely slow!
                    reordered_batch = utils.bottleneck_node_ordering(self.G, batch_arrived)
                else:
                    reordered_batch = batch_arrived
                #print('batch reordering', batch_arrived, reordered_batch)
                batch_arrived = reordered_batch
            else:
                batch_arrived = batch_arrived[::-1]

        # GRAPH MODIFICATION FUNCTIONS
        if self.graph_modification_functions:

            # set node weight to prediction generated from a GAM
            if self.alter_node_weight_to_gam_prediction:
                total_arrived = self.nodes_arrived + batch_arrived
                if len(total_arrived) < self.gam_k_value:
                    k = len(total_arrived)
                else:
                    k = self.gam_k_value

                gam_weights = utils.gam_predict(self.POPULATION_LOCATION_FILE,
                                                self.SIMULATED_ARRIVAL_FILE,
                                                len(total_arrived),
                                                k)

                for node in self.G.nodes_iter():
                    if self.alter_arrived_node_weight_to_100 and node in total_arrived:
                        pass # weight would have been set previously
                    else:
                        self.G.node[node]['weight'] = int(gam_weights[node] * 100)

            self.G = self._edge_expansion(self.G)

        self._batches.append(batch_arrived)

        # make a subgraph of all arrived nodes
        Gsub = self.G.subgraph(self.nodes_arrived + batch_arrived)

        # recalculate alpha
        if Gsub.is_directed():
            # as it's a directed graph, edges_arrived is actually double, so divide by 2
            edges_arrived = Gsub.number_of_edges() / 2
        else:
            edges_arrived = Gsub.number_of_edges()

        if self.PARTITIONER_ALGORITHM == 'FENNEL':
            nodes_fixed = len([o for o in self.fixed if o == 1])
            alpha = (edges_arrived) * (self.num_partitions / (nodes_fixed + len(batch_arrived))**2)

            if self.use_one_shot_alpha:
                alpha = self.one_shot_alpha

            self.partition_algorithm.PREDICTION_MODEL_ALPHA = alpha
            self.batch_node_order = batch_arrived

        if self.alter_node_weight_to_gam_prediction:
            # justification: the gam learns the entire population, so run fennal on entire population
            self.assignments = self.partition_algorithm.generate_prediction_model(self.G,
                                                                self.num_iterations,
                                                                self.num_partitions,
                                                                self.assignments,
                                                                self.fixed)

        else:
            # use the information we have, those that arrived
            self.assignments = self.partition_algorithm.generate_prediction_model(Gsub,
                                                                self.num_iterations,
                                                                self.num_partitions,
                                                                self.assignments,
                                                                self.fixed)

        if self.sliding_window and not assign_all:
            # assign first node
            n = batch_arrived.pop(0)
            self.fixed[n] = 1
            self.nodes_arrived.append(n)

        else:
            # assign all nodes to prediction model
            for n in batch_arrived:
                self.fixed[n] = 1
                self.nodes_arrived.append(n)

        if self.alter_node_weight_to_gam_prediction:
            # if GAM enabled, self.assignments has everyone who hasn't arrived yet partitioned, which is wrong for computing metrics
            # therefore, whoever hasn't arrived should be un-partitioned after GAM prediction model is computed earlier
            # if node is not fixed, then they haven't arrived, so we set their assignment back to -1
            for i in range(0, len(self.assignments)):
                # for each node, if not fixed, set assignment to -1
                if self.fixed[i] != 1:
                    self.assignments[i] = -1

        return [self._print_score(Gsub)]


    def get_metrics(self):
        self.clean_up()
        self.get_graph_metrics()
        self.get_partition_metrics()


    def clean_up(self):
        if nx.is_frozen(self.G):
            return

        if self.use_virtual_nodes:
            if self.verbose > 0:
                print("Remove virtual nodes")

                print("\nCurrent graph:")
                print("Nodes: {}".format(self.G.number_of_nodes()))
                print("Edges: {}".format(self.G.number_of_edges()))

            self.G.remove_nodes_from(self.virtual_nodes)
            self.assignments = np.delete(self.assignments, self.virtual_nodes)
            self.fixed = np.delete(self.fixed, self.virtual_nodes)

            if self.verbose > 0:
                print("\nVirtual nodes removed:")
                print("Nodes: {}".format(self.G.number_of_nodes()))
                print("Edges: {}".format(self.G.number_of_edges()))


        # Add partition attribute to nodes
        for i in range(0, len(self.assignments)):
            self.G.add_nodes_from([i], partition=str(self.assignments[i]))

        # Remove original node/edge weights
        for node in self.G.nodes_iter(data=True):
            if 'weight_orig' in node[1]:
                del node[1]['weight_orig']
        for edge in self.G.edges_iter(data=True):
            if 'weight_orig' in edge[2]:
                del edge[2]['weight_orig']

        # Freeze Graph from further modification
        self.G = nx.freeze(self.G)

    def compute_output_filenames(self):
        self.metrics_run_folder = os.path.join(self.OUTPUT_DIRECTORY, datetime.datetime.now().strftime('%y_%m_%d').replace("/", ""))
        f,_ = os.path.splitext(os.path.basename(self.DATA_FILENAME))
        self.metrics_run_filename_prediction = self.PREDICTION_MODEL_ALGORITHM + '_' + f + '-' + datetime.datetime.now().strftime('%H%M%S')
        self.metrics_run_filename_partitioner = self.PARTITIONER_ALGORITHM + '_' + f + '-' + datetime.datetime.now().strftime('%H%M%S')
        self.metrics_run_file_prefix_prediction = os.path.join(self.metrics_run_folder, self.metrics_run_filename_prediction)
        self.metrics_run_file_prefix_partitioner = os.path.join(self.metrics_run_folder, self.metrics_run_filename_partitioner)

    def get_graph_metrics(self):

        self.metrics_timestamp = datetime.datetime.now().strftime('%H%M%S')
        f,_ = os.path.splitext(os.path.basename(self.DATA_FILENAME))
        self.metrics_filename = f + "-" + self.metrics_timestamp

        graph_metrics = {
            "file": self.metrics_timestamp,
            "num_partitions": self.num_partitions,
            "num_iterations": self.num_iterations,
            "prediction_model_cut_off": self.prediction_model_cut_off,
            "restream_batches": self.restream_batches,
            "use_virtual_nodes": self.use_virtual_nodes,
            "virtual_edge_weight": self.virtual_edge_weight,
        }
        graph_fieldnames = [
            "file",
            "num_partitions",
            "num_iterations",
            "prediction_model_cut_off",
            "restream_batches",
            "use_virtual_nodes",
            "virtual_edge_weight",
            "edges_cut",
            "waste",
            "cut_ratio",
            "total_communication_volume",
            "network_permanence",
            "Q",
            "NQ",
            "Qds",
            "intraEdges",
            "interEdges",
            "intraDensity",
            "modularity degree",
            "conductance",
            "expansion",
            "contraction",
            "fitness",
            "QovL",
        ]

        if self.verbose > 0:
            print("Complete graph with {} nodes".format(self.G.number_of_nodes()))
        file_oslom = utils.write_graph_files(self.OUTPUT_DIRECTORY,
                                             "{}-all".format(self.metrics_filename),
                                             self.G,
                                             quiet=True)

        # original scoring algorithm
        scoring = utils.score(self.G, self.assignments, self.num_partitions)
        graph_metrics.update({
            "waste": scoring[0],
            "cut_ratio": scoring[1],
        })

        # edges cut and communication volume
        edges_cut, steps, cut_edges = utils.base_metrics(self.G, self.assignments)
        graph_metrics.update({
            "edges_cut": edges_cut,
            "total_communication_volume": steps,
        })

        # MaxPerm
        max_perm = utils.run_max_perm(self.G)
        graph_metrics.update({"network_permanence": max_perm})

        # Community Quality metrics
        community_metrics = utils.run_community_metrics(self.OUTPUT_DIRECTORY,
                                                        "{}-all".format(self.metrics_filename),
                                                        file_oslom)
        graph_metrics.update(community_metrics)

        if self.verbose > 0:
            print("\nConfig")
            print("-------\n")
            for f in graph_fieldnames[:8]:
                print("{}: {}".format(f, graph_metrics[f]))

            print("\nMetrics")
            print("-------\n")
            for f in graph_fieldnames[8:]:
                print("{}: {}".format(f, graph_metrics[f]))

        # write metrics to CSV
        csv_file = os.path.join(self.OUTPUT_DIRECTORY, "metrics.csv")
        utils.write_metrics_csv(csv_file, graph_fieldnames, graph_metrics)


    def get_partition_metrics(self):
        partition_nonoverlapping_metrics = {}
        partition_nonoverlapping_metrics = {}
        partition_nonoverlapping_fieldnames = [
            "file",
            "partition",
            "population",
            "modularity",
            "loneliness_score",
            "network_permanence",
        ]
        partition_overlapping_fieldnames = [
            "file",
            "partition",
            "population",
            "Q",
            "NQ",
            "Qds",
            "intraEdges",
            "interEdges",
            "intraDensity",
            "modularity degree",
            "conductance",
            "expansion",
            "contraction",
            "fitness",
            "QovL",
        ]

        partition_population = utils.get_partition_population(self.G, self.assignments, self.num_partitions)

        for p in range(0, self.num_partitions):
            partition_overlapping_metrics = {
                "file": self.metrics_timestamp,
                "partition": p,
                "population": partition_population[p][0]
            }
            partition_nonoverlapping_metrics = {
                "file": self.metrics_timestamp,
                "partition": p,
                "population": partition_population[p][0]
            }

            nodes = [i for i,x in enumerate(self.assignments) if x == p]
            Gsub = self.G.subgraph(nodes)
            if self.verbose > 0:
                print("\nPartition {} with {} nodes".format(p, Gsub.number_of_nodes()))
                print("-----------------------------\n")

            file_oslom = utils.write_graph_files(self.OUTPUT_DIRECTORY,
                                                 "{}-p{}".format(self.metrics_filename, p),
                                                 Gsub,
                                                 quiet=True)

            # MaxPerm
            max_perm = utils.run_max_perm(Gsub, relabel_nodes=True)
            partition_nonoverlapping_metrics.update({"network_permanence": max_perm})

            # Modularity
            mod = utils.modularity(Gsub, best_partition=True)
            partition_nonoverlapping_metrics.update({"modularity": mod})

            # Loneliness Score
            score = utils.loneliness_score(Gsub, self.loneliness_score_param)
            partition_nonoverlapping_metrics.update({"loneliness_score": score})

            # Community Quality metrics
            community_metrics = utils.run_community_metrics(self.OUTPUT_DIRECTORY,
                                                            "{}-p{}".format(self.metrics_filename, p),
                                                            file_oslom)
            partition_overlapping_metrics.update(community_metrics)

            if self.verbose > 0:
                print("\nMetrics")
                for f in partition_overlapping_fieldnames:
                    print("{}: {}".format(f, partition_overlapping_metrics[f]))

                for f in partition_nonoverlapping_fieldnames:
                    print("{}: {}".format(f, partition_nonoverlapping_metrics[f]))

            # write metrics to CSV
            csv_file = os.path.join(self.OUTPUT_DIRECTORY, "metrics-partitions-overlapping.csv")
            utils.write_metrics_csv(csv_file, partition_overlapping_fieldnames, partition_overlapping_metrics)

            csv_file = os.path.join(self.OUTPUT_DIRECTORY, "metrics-partitions-nonoverlapping.csv")
            utils.write_metrics_csv(csv_file, partition_nonoverlapping_fieldnames, partition_nonoverlapping_metrics)

    def apply_graph_prediction_weights(self):
        for i, weight in enumerate(self.predicted_displacement_weights):
            try:
                if(weight == 0):
                    weight = 1.0
                self.G.node[i]['weight'] = weight
            except Exception as err:
                pass

    def remove_graph_prediction_weights(self):
        for node in self.G.nodes():
            try:
                self.G.node[node]['weight'] = 1.0
            except Exception as err:
                pass
