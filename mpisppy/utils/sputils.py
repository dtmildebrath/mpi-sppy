# This software is distributed under the 3-clause BSD License.
# Base and utility functions for mpisppy
# Note to developers: things called spcomm are way more than just a comm; SPCommunicator

import pyomo.environ as pyo
import re
import time
from numpy import prod

from mpi4py import MPI
from pyomo.pysp.phutils import find_active_objective
from pyomo.core.expr.numeric_expr import LinearExpression

from mpisppy import tt_timer

def spin_the_wheel(hub_dict, list_of_spoke_dict, comm_world=None):
    """ top level for the hub and spoke system
    Args:
        hub_dict(dict): controls hub creation
        list_of_spoke_dict(list dict): controls creation of spokes
        comm_world (MPI comm): the world for this hub-spoke system

    Returns:
        spcomm (Hub or Spoke object): the object that did the work (windowless)
        opt_dict (dict): the dictionary that controlled creation for this rank

    NOTE: the return is after termination; the objects are provided for query.

    """
    # Confirm that the provided dictionaries specifying
    # the hubs and spokes contain the appropriate keys
    if "hub_class" not in hub_dict:
        raise RuntimeError(
            "The hub_dict must contain a 'hub_class' key specifying "
            "the hub class to use"
        )
    if "opt_class" not in hub_dict:
        raise RuntimeError(
            "The hub_dict must contain an 'opt_class' key specifying "
            "the SPBase class to use (e.g. PHBase, etc.)"
        )
    if "hub_kwargs" not in hub_dict:
        hub_dict["hub_kwargs"] = dict()
    if "opt_kwargs" not in hub_dict:
        hub_dict["opt_kwargs"] = dict()
    for spoke_dict in list_of_spoke_dict:
        if "spoke_class" not in spoke_dict:
            raise RuntimeError(
                "Each spoke_dict must contain a 'spoke_class' key "
                "specifying the spoke class to use"
            )
        if "opt_class" not in spoke_dict:
            raise RuntimeError(
                "Each spoke_dict must contain an 'opt_class' key "
                "specifying the SPBase class to use (e.g. PHBase, etc.)"
            )
        if "spoke_kwargs" not in spoke_dict:
            spoke_dict["spoke_kwargs"] = dict()
        if "opt_kwargs" not in spoke_dict:
            spoke_dict["opt_kwargs"] = dict()

    if comm_world is None:
        comm_world = MPI.COMM_WORLD
    n_spokes = len(list_of_spoke_dict)

    # Create the necessary communicators
    fullcomm = comm_world
    intercomm, intracomm = make_comms(n_spokes, fullcomm=fullcomm)
    rank_inter = intercomm.Get_rank()
    rank_intra = intracomm.Get_rank()
    rank_global = fullcomm.Get_rank()

    # Assign hub/spokes to individual ranks
    if rank_inter == 0: # This rank is a hub
        sp_class = hub_dict["hub_class"]
        sp_kwargs = hub_dict["hub_kwargs"]
        opt_class = hub_dict["opt_class"]
        opt_kwargs = hub_dict["opt_kwargs"]
        opt_dict = hub_dict
    else: # This rank is a spoke
        spoke_dict = list_of_spoke_dict[rank_inter - 1]
        sp_class = spoke_dict["spoke_class"]
        sp_kwargs = spoke_dict["spoke_kwargs"]
        opt_class = spoke_dict["opt_class"]
        opt_kwargs = spoke_dict["opt_kwargs"]
        opt_dict = spoke_dict

    # Create the appropriate opt object locally
    opt_kwargs["mpicomm"] = intracomm
    opt = opt_class(**opt_kwargs)

    # Create the SPCommunicator object (hub/spoke) with
    # the appropriate SPBase object attached
    if rank_inter == 0: # Hub
        spcomm = sp_class(opt, fullcomm, intercomm, intracomm,
                          list_of_spoke_dict, **sp_kwargs) 
    else: # Spokes
        spcomm = sp_class(opt, fullcomm, intercomm, intracomm, **sp_kwargs) 

    # Create the windows, run main(), destroy the windows
    spcomm.make_windows()
    if rank_inter == 0:
        spcomm.setup_hub()
    if rank_global == 0:
        tt_timer.toc("Starting spcomm.main()", delta=False)
    spcomm.main()
    if rank_inter == 0: # If this is the hub
        spcomm.send_terminate()

    # Anything that's left to do
    spcomm.finalize()

    if rank_global == 0:
        tt_timer.toc("Hub algorithm complete, waiting for termination barrier", delta=False)
    fullcomm.Barrier()

    ## give the hub the chance to catch new values
    spcomm.hub_finalize()

    spcomm.free_windows()
    if rank_global == 0:
        tt_timer.toc("Windows freed", delta=False)

    return spcomm, opt_dict
    
def make_comms(n_spokes, fullcomm=None):
    """ Create the intercomm and intracomm for hub/spoke style runs
    """
    # Ensure that the proper number of processes have been invoked
    nsp1 = n_spokes + 1 # Add 1 for the hub
    if fullcomm is None:
        fullcomm = MPI.COMM_WORLD
    n_proc = fullcomm.Get_size() 
    if n_proc % nsp1 != 0:
        raise RuntimeError(f"Need a multiple of {nsp1} processes (got {n_proc})")

    # Create the intercomm and intracomm
    # Cryptic comment: intra is vertical, inter is around the hub
    rank_global = fullcomm.Get_rank()
    intercomm = fullcomm.Split(key=rank_global, color=rank_global // nsp1)
    intracomm = fullcomm.Split(key=rank_global, color=rank_global % nsp1)
    return intercomm, intracomm


def get_objs(scenario_instance):
    """ return the list of objective functions for scenario_instance"""
    scenario_objs = scenario_instance.component_data_objects(pyo.Objective,
                    active=True, descend_into=True)
    scenario_objs = list(scenario_objs)
    if (len(scenario_objs) == 0):
        raise RuntimeError("Scenario " + sname + " has no active "
                           "objective functions.")
    if (len(scenario_objs) > 1):
        print("WARNING: Scenario", sname, "has multiple active "
              "objectives. Selecting the first objective for "
                  "inclusion in the extensive form.")
    return scenario_objs

def create_EF(scenario_names, scenario_creator, creator_options=None,
              EF_name=None, suppress_warnings=False):
    """ Create a ConcreteModel of the extensive form

        Args:
            scenario_names (list of str): Names for each scenario, to be passed
                to the scenario_creator function.
            scenario_creator (callable): Function which takes a scenario name
                as its first argument and returns a concrete model
                corresponding to that scenario.
            creator_options (dict--optional): options to be passed when
                scenario_creator is called.
            EF_name (str--optional): name of the ConcreteModel of the EF.
            suppress_warnings (boolean): don't warn about things

        Returns:
            EF_instance (ConcreteModel): ConcreteModel of extensive form with
                explicit non-anticipativity constraints.

        Notes:
            If any of the scenarios produced by scenario_creator do not have a
            .PySP_prob attribute, this function displays a warning, and assumes
            that all scenarios are equally likely.
    """
    if (creator_options is None):
        creator_options = dict()
    scen_dict = {name: scenario_creator(name, **creator_options)
                    for name in scenario_names}

    if (len(scen_dict) == 0):
        raise RuntimeError("create_EF() received empty scenario list")
    elif (len(scen_dict) == 1):
        scenario_instance = list(scen_dict.values())[0]
        if not suppress_warnings:
            print("WARNING: passed single scenario to create_EF()")
        # special code to patch in ref_vars
        scenario_instance.ref_vars = dict()
        for node in scenario_instance._PySPnode_list:
            ndn = node.name
            nlens = {node.name: len(node.nonant_vardata_list) 
                                for node in scenario_instance._PySPnode_list}
            for i in range(nlens[ndn]):
                v = node.nonant_vardata_list[i]
                if (ndn, i) not in scenario_instance.ref_vars:
                    scenario_instance.ref_vars[(ndn, i)] = v
        # patch in EF_Obj        
        scenario_objs = get_objs(scenario_instance)        
        for obj_func in scenario_objs:
            obj_func.deactivate()
        obj = scenario_objs[0]            
        sense = pyo.minimize if obj.is_minimizing() else pyo.maximize
        scenario_instance.EF_Obj = pyo.Objective(expr=obj.expr, sense=sense)

        return scenario_instance  #### special return for single scenario

    # Check if every scenario has a specified probability
    probs_specified = \
        all([hasattr(scen, 'PySP_prob') for scen in scen_dict.values()])
    if not probs_specified:
        for scen in scen_dict.values():
            scen.PySP_prob = 1 / len(scen_dict)
        if not suppress_warnings:
            print('WARNING: At least one scenario is missing PySP_prob attribute.',
                  'Assuming equally-likely scenarios...')

    EF_instance = _create_EF_from_scen_dict(scen_dict, EF_name=EF_name)
    return EF_instance

def _create_EF_from_scen_dict(scen_dict, EF_name=None,
                                nonant_for_fixed_vars=True):
    """ Create a ConcreteModel of the extensive form from a scenario
        dictionary.

        Args:
            scen_dict (dict): Dictionary whose keys are scenario names and
                values are ConcreteModel objects corresponding to each
                scenario.
            EF_name (str--optional): Name of the resulting EF model.
            nonant_for_fixed_vars (bool--optional): If True, enforces
                non-anticipativity constraints for all variables, including
                those which have been fixed. Deafult is True.

        Returns:
            EF_instance (ConcreteModel): ConcreteModel of extensive form with
                explicity non-anticipativity constraints.

        Notes:
            The non-anticipativity constraints are enforced by creating
            "reference variables" at each node in the scenario tree (excluding
            leaves) and enforcing that all the variables for each scenario at
            that node are equal to the reference variables.

            This function is called directly when creating bundles for PH.
 
            Does NOT assume that each scenario is equally likely. Raises an
            AttributeError if a scenario object is encountered which does not
            have a .PySP_prob attribute.

            Added the flag nonant_for_fixed_vars because original code only
            enforced non-anticipativity for non-fixed vars, which is not always
            desirable in the context of bundling. This allows for more
            fine-grained control.
    """
    is_min, clear = _models_have_same_sense(scen_dict)
    if (not clear):
        raise RuntimeError('Cannot build the extensive form out of models '
                           'with different objective senses')
    sense = pyo.minimize if is_min else pyo.maximize
    EF_instance = pyo.ConcreteModel(name=EF_name)
    EF_instance.EF_Obj = pyo.Objective(expr=0.0, sense=sense)
    EF_instance._PySP_feas_indicator = None
    EF_instance._PySP_subscen_names = []
    EF_instance.PySP_prob = 0
    for (sname, scenario_instance) in scen_dict.items():
        EF_instance.add_component(sname, scenario_instance)
        EF_instance._PySP_subscen_names.append(sname)
        # Now deactivate the scenario instance Objective
        scenario_objs = get_objs(scenario_instance)
        for obj_func in scenario_objs:
            obj_func.deactivate()
        obj_func = scenario_objs[0] # Select the first objective
        try:
            EF_instance.EF_Obj.expr += scenario_instance.PySP_prob * obj_func.expr
            EF_instance.PySP_prob   += scenario_instance.PySP_prob
        except AttributeError as e:
            raise AttributeError("Scenario " + sname + " has no specified "
                        "probability. Specify a value for the attribute "
                        " PySP_prob and try again.") from e
    # Normalization does nothing when solving the full EF, but is required for
    # appropraite scaling of EFs used as bundles.
    EF_instance.EF_Obj.expr /= EF_instance.PySP_prob

    # For each node in the scenario tree, we need to collect the
    # nonanticipative vars and create the constraints for them,
    # which we do using a reference variable.
    ref_vars = dict() # keys are _nonant_indexes (i.e. a node name and a
                      # variable number)

    ref_suppl_vars = dict()

    EF_instance._PySP_nlens = dict() 

    nonant_constr = pyo.Constraint(pyo.Any, name='_C_EF_')
    EF_instance.add_component('_C_EF_', nonant_constr)


    nonant_constr_suppl = pyo.Constraint(pyo.Any, name='_C_EF_suppl')
    EF_instance.add_component('_C_EF_suppl', nonant_constr_suppl)

    for (sname, s) in scen_dict.items():
        if (not hasattr(s, '_PySP_nlens')):
            nlens = {node.name: len(node.nonant_vardata_list) 
                                for node in s._PySPnode_list}
        else:
            nlens = s._PySP_nlens
        
        for (node_name, num_nonant_vars) in nlens.items(): # copy nlens to EF
            if (node_name in EF_instance._PySP_nlens.keys() and
                num_nonant_vars != EF_instance._PySP_nlens[node_name]):
                raise RuntimeError("Number of non-anticipative variables is "
                    "not consistent at node " + node_name + " in scenario " +
                    sname)
            EF_instance._PySP_nlens[node_name] = num_nonant_vars

        nlens_ef_suppl = {node.name: len(node.nonant_ef_suppl_vardata_list)
                                   for node in s._PySPnode_list}

        for node in s._PySPnode_list:
            ndn = node.name
            for i in range(nlens[ndn]):
                v = node.nonant_vardata_list[i]
                if (ndn, i) not in ref_vars:
                    # create the reference variable as a singleton with long name
                    # xxxx maybe index by _nonant_index ???? rather than singleton VAR ???
                    ref_vars[(ndn, i)] = v
                # Add a non-anticipativity constraint, except in the case when
                # the variable is fixed and nonant_for_fixed_vars=False.
                else:
                    if (nonant_for_fixed_vars):
                        expr = LinearExpression(linear_coefs=[1,-1],
                                                linear_vars=[v,ref_vars[(ndn,i)]],
                                                constant=0.)
                        nonant_constr[(ndn,i,sname)] = (expr, 0.0)
                    elif (not v.is_fixed()):
                        expr = LinearExpression(linear_coefs=[1,-1],
                                                linear_vars=[v,ref_vars[(ndn,i)]],
                                                constant=0.)
                        nonant_constr[(ndn,i,sname)] = (expr, 0.0)

            for i in range(nlens_ef_suppl[ndn]):
                v = node.nonant_ef_suppl_vardata_list[i]
                if (ndn, i) not in ref_suppl_vars:
                    # create the reference variable as a singleton with long name
                    # xxxx maybe index by _nonant_index ???? rather than singleton VAR ???
                    ref_suppl_vars[(ndn, i)] = v
                # Add a non-anticipativity constraint, expect in the case when
                # the variable is fixed and nonant_for_fixed_vars=False.
                else:
                    if (nonant_for_fixed_vars):
                        expr = LinearExpression(linear_coefs=[1,-1],
                                                linear_vars=[v,ref_suppl_vars[(ndn,i)]],
                                                constant=0.)
                        nonant_constr_suppl[(ndn,i,sname)] = (expr, 0.0)
                    elif (not v.is_fixed()):
                        expr = LinearExpression(linear_coefs=[1,-1],
                                                linear_vars=[v,ref_suppl_vars[(ndn,i)]],
                                                constant=0.)
                        nonant_constr_suppl[(ndn,i,sname)] = (expr, 0.0)

    EF_instance.ref_vars = ref_vars
    EF_instance.ref_suppl_vars = ref_suppl_vars
                        
    return EF_instance

def _models_have_same_sense(models):
    ''' Check if every model in the provided dict has the same objective sense.

        Input:
            models (dict) -- Keys are scenario names, values are Pyomo
                ConcreteModel objects.
        Returns:
            is_minimizing (bool) -- True if and only if minimizing. None if the
                check fails.
            check (bool) -- True only if all the models have the same sense (or
                no models were provided)
        Raises:
            ValueError -- If any of the models has either none or multiple
                active objectives.
    '''
    if (len(models) == 0):
        return True, True
    senses = [find_active_objective(scenario, True).is_minimizing()
                for scenario in models.values()]
    sense = senses[0]
    check = all(val == sense for val in senses)
    if (check):
        return (sense == pyo.minimize), check
    return None, check

def is_persistent(solver):
    return isinstance(solver,
        pyo.pyomo.solvers.plugins.solvers.persistent_solver.PersistentSolver)

def extract_num(string):
    ''' Given a string, extract the longest contiguous
        integer from the right-hand side of the string.

        Example:
            scenario324 -> 324

        TODO: Add Exception Handling
    '''
    return int(re.compile(r'(\d+)$').search(string).group(1))


def ef_nonants(ef):
    """ An iterator to give representative Vars subject to non-anticipitivity
    Args:
        ef (ConcreteModel): the full extensive form model

    Yields:
        tree node name, full EF Var name, Var value
    """
    for key, val in ef.ref_vars.items():
        yield (key[0], val, pyo.value(val))

        
def ef_nonants_csv(ef, filename):
    """ Dump the nonant vars from an ef to a csv file; truly a dump...
    Args:
        ef (ConcreteModel): the full extensive form model
        filename (str): the full name of the csv output file
    """
    with open(filename, "w") as outfile:
        outfile.write("Node, EF_VarName, Value\n")
        for (ndname, varname, varval) in ef_nonants(ef):
            outfile.write("{}, {}, {}\n".format(ndname, varname, varval))

            
def ef_scenarios(ef):
    """ An iterator to give the scenario sub-models in an ef
    Args:
        ef (ConcreteModel): the full extensive form model

    Yields:
        scenario name, scenario instance (str, ConcreteModel)
    """
    
    for sname in ef._PySP_subscen_names:
        yield (sname, getattr(ef, sname))

        
def option_string_to_dict(ostr):
    """ Convert a string to the standard dict for solver options.
    Intended for use in the calling program; not internal use here.

    Args:
        ostr (string): space seperated options with = for arguments

    Returns:
        solver_options (dict): solver options

    """
    def convert_value_string_to_number(s):
        try:
            return float(s)
        except ValueError:
            try:
                return int(s)
            except ValueError:
                return s

    solver_options = dict()
    if ostr is None or ostr == "":
        return None
    for this_option_string in ostr.split():
        this_option_pieces = this_option_string.strip().split("=")
        if len(this_option_pieces) == 2:
            option_key = this_option_pieces[0]
            option_value = convert_value_string_to_number(this_option_pieces[1])
            solver_options[option_key] = option_value
        elif len(this_option_pieces) == 1:
            option_key = this_option_pieces[0]
            solver_options[option_key] = None
        else:
            raise RuntimeError("Illegally formed subsolve directive"\
                               + " option=%s detected" % this_option)
    return solver_options


################################################################################
# Various utilities related to scenario rank maps (some may not be in use)


def scens_to_ranks(scen_count, n_proc, rank, BFs = None):
    """ Determine the rank assignments that are made in spbase.
    Args:
        scen_count (int): number of scenarios
        n_proc (int): the number of intra ranks (within the cylinder)
        rank (int): my rank (i.e., intra; i.e., within the cylinder)
    Returns:
        slices (list of ranges): the indexes into all all_scenario_names to assign to rank
                                 (the list entries are ranges that correspond to ranks)
        scenario_name_to_rank (dict of dict): only for multi-stage
                keys are comms (i.e., tree nodes); values are dicts with keys
                that are scenario names and values that are ranks

    """
    if scen_count < n_proc:
        raise RuntimeError(
            "More MPI ranks (%d) supplied than needed given the number of scenarios (%d) "
            % (n_proc, scen_count)
        )

    # for now, we are treating two-stage as a special case
    if (BFs is None):
        avg = scen_count / n_proc
        slices = [range(int(i * avg), int((i + 1) * avg)) for i in range(n_proc)]
        return slices, None
    else:
        # indecision as of May 2020 (delete this comment DLW)
        # just make sure things are consistent with what xhat will do...
        # TBD: streamline
        all_scenario_names = ["ID"+str(i) for i in range(scen_count)]
        tree = _ScenTree(BFs, all_scenario_names)
        scenario_names_to_ranks, slices, _ = tree.scen_name_to_rank(n_proc, rank)
        return slices, scenario_names_to_ranks

class _TreeNode():
    # everything is zero based, even stage numbers (perhaps not used)
    # scenario lists are stored as (first, last) indexes in all_scenarios
    def __init__(self, Parent, scenfirst, scenlast, BFs, name):
        self.scenfirst = scenfirst
        self.scenlast = scenlast
        self.name = name
        if Parent is None:
            assert(self.name == "ROOT")
            self.stage = 0
        else:
            self.stage = Parent.stage + 1
        # make children
        self.kids = list()
        bf = BFs[self.stage]        
        if self.stage < len(BFs)-1:
            numscens = scenlast - scenfirst + 1
            for b in range(bf):
                first  = b*numscens // bf
                last = ((b+1)*numscens // bf) - 1
                self.kids.append(_TreeNode(self,
                                           first, last,
                                           BFs,
                                           self.name+'_'+str(b)))

                
class _ScenTree():
    #  (perhaps not used)
    def __init__(self, BFs, ScenNames):
        self.ScenNames = ScenNames
        self.NumScens = len(ScenNames)
        assert(self.NumScens == prod(BFs))
        self.NumStages = len(BFs)
        assert(self.NumStages > 1)
        self.BFs = BFs
        first = 0
        last = self.NumScens - 1
        self.rootnode = _TreeNode(None, first, last, BFs, "ROOT")
        def _nonleaves(nd):
            retval = [nd]
            if nd.stage < len(BFs) - 1:
                for kid in nd.kids:
                    retval += _nonleaves(kid)
            return retval
        self.nonleaves = _nonleaves(self.rootnode)
        self.NonLeafTerminals = [nd for nd in self.nonleaves if nd.stage == len(BFs) - 1]

    def scen_names_to_ranks(self, n_proc, myrank):
        """ return scenario_name_to_rank (dict of dict): nodes (i.e. comms) scen names
                keys are comms (i.e., tree nodes); values are dicts with keys
                that are scenario names and values that are ranks

        """
        retval = dict()
        if n_proc == 1:
            for nd in self.nonleaves:
                retval[nd.name] = {s: myrank for s in self.ScenNames}
            return retval, None, None

        # First we assign ranks to terminal, non-leaf nodes.
        TermNodes = self.NonLeafTerminals
        FirstRank = dict()  # for each node, what is the first (intra) rank        
        # non-trivial assignment
        avg = n_proc / len(TermNodes)
        if avg < 1:
            raise RuntimeError("{} processors in a cylinder is not enough for {} terminal non-leaf nodes".\
                               format(n_proc, len(TermNodes)))
        TermRanks = [range(int(i*avg), int((i+1)*avg)) for i in range(len(TermNodes))]
        ##print("debug TermRanks=",TermRanks)
        FirstRank = {nd.name: int(i*avg) for i, nd in enumerate(TermNodes)}
        ##print("debug ... so far FirstRank=",FirstRank)
        # now assign scenarios to ranks

        # Also create a compatible list of slices (indexes correspond to ranks
        # and the values are ranges first and last scen number
        # The use of these slices is an artifact from two-stage
        masterslices = list()

        FirstRank["ROOT"] = 0
        retval["ROOT"] = dict()
        for i, nd in enumerate(TermNodes):
            retval[nd.name] = dict()
            ndnumscens = nd.scenlast - nd.scenfirst + 1
            ndScenList = [self.ScenNames[s] for s in range(nd.scenfirst, nd.scenlast+1)]
            avg = ndnumscens / len(TermRanks[i])  # scens per rank
            # slice[k] gives list of relative scenario numbers for rank k
            slices = [range(int(j*avg), int((j+1)*avg)) for j in range(len(TermRanks[i]))]
            # master slices wants almost the same, but not relative
            masterslices.append([range(nd.scenfirst+int(j*avg), nd.scenfirst+int((j+1)*avg)) for j in range(len(TermRanks[i]))])
            for k, slist in enumerate(slices):
                for s in slist:
                    retval[nd.name][ndScenList[s]] = TermRanks[i][k]
                    retval["ROOT"][ndScenList[s]] = TermRanks[i][k]
        # now it is easy to assign the rest of the scenario tree
        for nd in self.nonleaves:
            if nd not in TermNodes and nd.name != "ROOT":
                retval[nd.name] = dict()
                ndScenList = [self.ScenNames[s] for s in range(nd.scenfirst, nd.scenlast+1)]
                for s in ndScenList:
                    retval[nd.name][s] = retval["ROOT"][s]
                # TBD (May 2020): test this
                firstchild = nd.kids[0]
                FirstRank[nd.name] = FirstRank[firstchild.name]

        return retval, masterslices, FirstRank

"""
if __name__ == "__main__":
    BFs = [2,3,3]
    scennames = ["Scenario"+str(i) for i in range(prod(BFs))]
    testtree = _ScenTree(BFs, scennames)
    print("nonleaves:")
    for nd in testtree.nonleaves:
        print("   ", nd.name)
    print("NonLeafTerminals:")
    for nd in testtree.NonLeafTerminals:
        print("   ", nd.name)
    n_proc = 8
    sntr = testtree.scen_name_to_rank(n_proc)
    print("map:")
    for ndn,v in sntr.items():
        print(ndn, v)
"""
