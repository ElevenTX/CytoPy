# Dependencies
# Immunova.data
from ...data.gating import Gate as DataGate, GatingStrategy
from ...data.fcs import FileGroup, Population
from ...data.fcs_experiments import FCSExperiment
# Immunova.flow
from ..transforms import apply_transform
from .base import GateError
from .static import Static
from .fmo import FMOGate
from .density import DensityThreshold
from .dbscan import DensityBasedClustering
from .quantile import Quantile
from .mixturemodel import MixtureModel
from .defaults import ChildPopulationCollection
from .plotting.static_plots import Plot
from .utilities import get_params, inside_ellipse, inside_polygon
# Housekeeping and other tools
from anytree.exporter import DotExporter
from anytree import Node, RenderTree
from anytree.search import findall
from scipy.spatial import ConvexHull
from shapely.geometry.polygon import Polygon
from datetime import datetime
from copy import deepcopy
import inspect
import copy
# Scipy
from sklearn.neighbors import KNeighborsClassifier
import pandas as pd
import numpy as np


class Gating:
    """
    Central class for performing semi-automated gating and storing gating information on an FCS FileGroup of a single sample.

    Arguments:
        experiment - FCSExperiment you're currently working on
        sample_id - name of the sample to analyse (must belong to experiment)
        sample - number of events to sample from FCS file(s) (optional)
        include_controls - if True and FMOs are inclued for specified samples, the FMO data will also be loaded into
        the Gating object

    Attributes:
        id - the sample ID
        mongo_id - the document ID for the loaded sample
        experiment - an instance of the associated FCSExperiment
        plotting - instance of Plot object (seeo gating.plotting.static_plots)
        fmo_search_cache - dictionary of cached index of populations in FMO data (see method 'get_fmo_data')
        filegroup - instance of FileGroup object for associated sample
        gates - dictionary of Gate objects; contains all gate information
        populations dictionary of population Nodes (anytree.Node); contains all population information
        gating_classes - dictionary of available gating classes

    Methods:
        clear_gates - removes all existing gates
        fetch_geom - retrieve the geom that defined a given population
        population_size - returns in integer count for the number of events in a given population
        get_population_df - retrieve a population as a pandas dataframe
        valid_populations - given a list of populations, check validity and return list of valid populations
        search_fmo_cache - given the name of some desired population and an FMO of interest,
        check the FMO cache to determine if this population has been estimated for the FMO previously,
        if so, return the cached index.
        get_fmo_gate - given some target population that has already been defined in the primary data, predict the same
        population in a given FMO control. Following the gating strategy, each population from the root until the
        target population is predicted using a KNN model trained on the primary data (the FMO is assumed to have
        been collected under the same experimental conditions and therefore should not significantly deviate).
        A dynamic programming methodology is taken where by predictions are cached for future use.
        Note: currently fmo cache is not saved to the database and must be re-calculated for each instance of Gating,
        future releases will offer the ability to save FMO cache
        subtraction - given a target population and a parent population, generate a new population by subtraction of the
        target population from the parent
        create_gate - define a new gate to be used using 'apply' method
        apply - given the name of an existing gate previously defined, apply the gate and store result internally
        apply_many - apply multiple existing gates sequentially
        update_populations - given some ChildPopulationCollection object generated from a gate, update saved populations
        edit_gate - manually replace the outcome of a gate by updating the geom of it's child populations.
        find_dependencies - for a given population return a list of populations downstream
        remove_population - remove given population from population tree
        remove_gate - remove gate from Gating object
        print_population_tree - print the population tree in a hierarchical format
        save - save all gates and population's to mongoDB
    """
    def __init__(self, experiment: FCSExperiment, sample_id: str, sample: int or None = None,
                 include_controls=True, default_axis='FSC-A'):
        """
        Constructor for Gating
        :param experiment: FCSExperiment currently being processed
        :param sample_id: Identifier of sample of interest
        :param sample: if an integer value is supplied then data will be sampled to this size. Optional (default = None)
        """
        try:
            data = experiment.pull_sample_data(sample_id=sample_id, sample_size=sample,
                                               include_controls=include_controls)
            assert data is not None
        except AssertionError:
            raise GateError(f'Error: failed to fetch data for {sample_id}. Aborting.')
        self.data = [x for x in data if x['typ'] == 'complete'][0]['data']
        if include_controls:
            self.fmo = [x for x in data if x['typ'] == 'control']
            self.fmo = {x['id'].replace(f'{sample_id}_', ''): x['data'] for x in self.fmo}
        else:
            self.fmo = {}
        self.id = sample_id
        self.mongo_id = experiment.fetch_sample_mid(sample_id)
        self.experiment = experiment
        self.plotting = Plot(self, default_axis)
        self.fmo_search_cache = {_id: dict(root=data.index.values) for _id, data in self.fmo.items()}
        del data
        fg = experiment.pull_sample(sample_id)
        self.filegroup = fg
        self.gates = dict()
        if fg.gates:
            for g in fg.gates:
                self._deserialise_gate(g)

        self.populations = dict()
        if not fg.populations or len(fg.populations) == 1:
            root = Node(name='root', prop_of_parent=1.0, prop_of_total=1.0,
                        warnings=[], geom=dict(shape=None, x='FSC-A', y='SSC-A'), index=self.data.index.values,
                        parent=None)
            self.populations['root'] = root
        else:
            # Reconstruct tree
            assert 'root' in [p.population_name for p in fg.populations], 'Root population missing!'
            parents = [p.parent for p in fg.populations]
            pops = [p.to_python() for p in fg.populations]
            structured = dict()
            for parent in parents:
                structured[parent] = [p for p in pops if p.get('parent') == parent]
            self.populations = dict()
            for name, pops in structured.items():
                for p in pops:
                    parent = p.pop('parent', None)
                    if not parent:
                        self.populations[p.get('name')] = Node(**p)
                    else:
                        self.populations[p.get('name')] = Node(**p, parent=self.populations.get(parent))

    def clear_gates(self):
        """
        Remove all currently associated gates.
        """
        self.gates = dict()

    def fetch_geom(self, population: str) -> dict:
        """
        Given the name of a population, retrieve the geom that defined this population
        :param population: name of population to be fetched
        :return: Population geom (dictionary)
        """
        assert population in self.populations.keys(), f'Population invalid, valid population names: ' \
                                                      f'{self.populations.keys()}'
        return copy.deepcopy(self.populations[population].geom)

    def population_size(self, population: str):
        """
        Returns in integer count for the number of events in a given population
        :param population: population name
        :return: event count
        """
        assert population in self.populations.keys(), f'Population invalid, valid population names: ' \
                                                      f'{self.populations.keys()}'
        return len(self.populations[population].index)

    def _deserialise_gate(self, gate):
        """
        Given some Gate document from the database, deserialise for use; re-instantiate ChildPopulationCollection
        :param gate: Gate object to deserialise
        :return: None
        """
        kwargs = {k: v for k, v in gate.kwargs}
        kwargs['child_populations'] = ChildPopulationCollection(json_dict=kwargs['child_populations'])
        gate.kwargs = [[k, v] for k, v in kwargs.items()]
        self.gates[gate.gate_name] = gate

    @staticmethod
    def _serailise_gate(gate):
        """
        Given some Gate document, serialise so that it can be saved to the database
        :param gate: Gate object to serialise
        :return: New 'database friendly' Gate
        """
        gate = deepcopy(gate)
        kwargs = {k: v for k, v in gate.kwargs}
        kwargs['child_populations'] = kwargs['child_populations'].serialise()
        gate.kwargs = [[k, v] for k, v in kwargs.items()]
        return gate

    @property
    def gating_classes(self) -> dict:
        """
        Available gating classes
        :return: Class look-up dictionary
        """
        available_classes = [Static, FMOGate, DensityBasedClustering, DensityThreshold, Quantile, MixtureModel]
        return {x.__name__: x for x in available_classes}

    def get_population_df(self, population_name: str, transform: bool = False,
                          transform_method: str or None = 'logicle',
                          transform_features: list or str = 'all',
                          label: bool =False) -> pd.DataFrame or None:
        """
        Retrieve a population as a pandas dataframe
        :param population_name: name of population to retrieve
        :param transform: if True, the provided transformation method will be applied to the returned dataframe
        (default = False)
        :param transform_method: transformation method to apply, default = 'logicle' (ignored if transform is False)
        :param transform_features: argument specifying which columns to transform in the returned dataframe. Can either
        be a string value of 'all' (transform all columns), 'fluorochromes' (transform all columns corresponding to a
        fluorochrome) or a list of valid column names
        :return: Population dataframe
        """
        if population_name not in self.populations.keys():
            print(f'Population {population_name} not recognised')
            return None
        idx = self.populations[population_name].index
        data = self.data.loc[idx]
        if label:
            data['label'] = None
            dependencies = self.find_dependencies(population_name)
            for pop in dependencies:
                data.loc[self.populations[pop].index, 'label'] = pop
        if transform_method is None:
            transform = False
        if transform:
            return apply_transform(data, features_to_transform=transform_features, transform_method=transform_method)
        return data

    def valid_populations(self, populations: list, verbose: bool = True) -> list:
        """
        Given a list of populations, check validity and return list of valid populations
        :param populations: list of populations to check
        :param verbose: if True, prints invalid population
        :return: Valid populations
        """
        valid = list()
        for pop in populations:
            if pop not in self.populations.keys():
                if verbose:
                    print(f'{pop} is not a valid population')
            else:
                valid.append(pop)
        return valid

    def search_fmo_cache(self, target_population: str, fmo: str) -> list or None:
        """
        Given the name of some desired population and an FMO of interest, check the FMO cache to determine
        if this population has been estimated for the FMO previously, if so, return the cached index.
        :param target_population: name of desired population
        :param fmo: name of FMO of interest
        :return: FMO population index or None if non-existing
        """
        if target_population in self.fmo_search_cache[fmo].keys():
            return self.fmo_search_cache[fmo][target_population]
        return None

    def get_fmo_data(self, target_population: str, fmo: str, sml_profiles: dict or None) -> pd.DataFrame:
        """
        Given some target population that has already been defined in the primary data, predict the same population
        in a given FMO control. Following the gating strategy, each population from the root until the target population
        is predicted using a KNN model trained on the primary data (the FMO is assumed to have been collected under
        the same experimental conditions and therefore should not significantly deviate).
        A dynamic programming methodology is taken where by predictions are cached for future use.
        Note: currently fmo cache is not saved to the database and must be re-calculated for each instance of Gating,
        future releases will offer the ability to save FMO cache
        :param target_population: name of target population to predict in FMO data (all upstream populations will also
        be predicted and cached)
        :param fmo: name of target FMO control
        :param sml_profiles: if one or more populations were derived using supervised machine learning methods in the
        primary data, you must provide a dictionary where each population is associated with geometric information
        defining what dimensions to isolate the population in e.g. {'XGBoost_CD4+Tcells': {'x': 'CD4', 'y': 'CD8'}}
        the KNN model would be trained using CD4 and CD8 as input features with the target being the CD4+Tcell
        population.
        :return: Pandas DataFrame for target population in FMO data
        """
        # Check cache if this population has been derived previously
        cache_idx = self.search_fmo_cache(target_population, fmo)
        if cache_idx is not None:
            return self.fmo[fmo].loc[cache_idx]
        else:
            cache_idx = self.fmo_search_cache[fmo]['root']

        node = self.populations[target_population]
        route = [x.name for x in node.path][1:]

        # Find start position by searching cache
        for i, pop in enumerate(route[::-1]):
            if pop in self.fmo_search_cache.keys():
                route = route[::-1][:i+1][::-1]
                cache_idx = self.populations[pop].index
                break

        fmo_data = self.fmo[fmo].loc[cache_idx]
        # Predict FMO index
        for pop in route:
            fmo_data = self.fmo[fmo].loc[cache_idx]

            # Train KNN from whole panel data
            x = self.populations[pop].geom['x']
            y = self.populations[pop].geom['y'] or 'FSC-A'

            # Check if SML gate, if so, check sml_profiles
            if self.populations[pop].geom['shape'] == 'sml':
                assert sml_profiles, f'No SML profiles provide yet population {pop} is sml defined'
                assert pop in sml_profiles.keys(), f'SML defined {pop} missing from sml_profiles'
                geom = sml_profiles.get(pop)
                x, y = geom.get('x'), geom.get('y')

            parent = self.populations[pop].parent.name
            train = self.get_population_df(parent)[[x, y]].copy()
            train['pos'] = 0
            if train.shape[0] > 10000:
                train = train.sample(10000)
            train.pos = train.pos.mask(train.index.isin(self.populations[pop].index), 1)
            y_ = train.pos.values
            knn = KNeighborsClassifier(n_jobs=-1, algorithm='ball_tree', n_neighbors=5)
            knn.fit(train[[x, y]], y_)

            # Predict population in FMO
            y_hat = knn.predict(fmo_data[[x, y]])
            fmo_data['pos'] = y_hat
            cache_idx = fmo_data[fmo_data['pos'] == 1].index.values
            self.fmo_search_cache[fmo][pop] = cache_idx
        return fmo_data.loc[cache_idx]

    @staticmethod
    def __check_class_args(klass, method: str, **kwargs) -> bool:
        """
        Check parameters meet class requirements
        :param klass: Valid gating class
        :param method: Name of class method to be called
        :param kwargs: Keyword arguments supplied by user
        :return: True if valid, else False
        """
        try:
            if not inspect.getmro(klass):
                raise GateError(f'{klass} Invalid: must inherit from Gate class')
            klass_args = get_params(klass, required_only=True, exclude_kwargs=True)
            for arg in klass_args:
                if arg in ['data']:
                    continue
                if arg not in kwargs.keys():
                    print(f'Error: missing required class constructor argument {arg} '
                          f'for gating class {klass.__name__}')
                    return False
            method_args = [k for k, v in inspect.signature(getattr(klass, method)).parameters.items()
                           if v.default is inspect.Parameter.empty]
            for arg in method_args:
                if arg == 'self':
                    continue
                if arg not in kwargs.keys():
                    print(f'Error: missing required method argument {arg} for method '
                          f'{method} belonging to {klass.__name__}')
                    return False
            return True
        except AttributeError:
            print(f'Error: {method} is not a valid method for class {klass.__name__}')
            return False

    def merge(self, population_left: str, population_right: str, new_population_name: str):
        assert new_population_name not in self.populations.keys(), f'{new_population_name} already exists!'
        assert population_left in self.populations.keys(), f'{population_left} not recognised!'
        assert population_right in self.populations.keys(), f'{population_right} not recognised!'
        population_left_parent = self.populations[population_left].parent.name
        population_right_parent = self.populations[population_right].parent.name
        assert population_left_parent == population_right_parent, 'Population parent must match for merging ' \
                                                                  'populations ' \
                                                                  f'left parent = {population_left_parent}, ' \
                                                                  f'right parent = {population_right_parent}'
        parent = self.populations[population_left_parent]
        x, y = self.populations[population_left].geom['x'], self.populations[population_left].geom['y']
        left_idx, right_idx = self.populations[population_left].index, self.populations[population_right].index
        index = np.unique(np.concatenate((left_idx, right_idx)))
        new_population = ChildPopulationCollection(gate_type='merge')
        new_population.add_population(new_population_name)
        parent_df = self.get_population_df(parent.name)
        d = parent_df.loc[index][[x, y]]
        hull = ConvexHull(d)
        polygon = Polygon([(d.values[v, 0], d.values[v, 1]) for v in hull.vertices])
        cords = dict(x=polygon.exterior.xy[0], y=polygon.exterior.xy[1])
        new_population.populations[new_population_name].update_geom(x=x, y=y, shape='poly', cords=cords)
        new_population.populations[new_population_name].update_index(index)
        self.update_populations(output=new_population, parent_df=parent_df,
                                parent_name=parent.name, warnings=[])
        name = f'merge_{population_left}_{population_right}'
        kwargs = [('left', population_left), ('right', population_right), ('name', new_population_name),
                  ('x', x), ('y', y), ('child_populations', new_population)]
        new_gate = DataGate(gate_name=name, children=list(new_population.populations.keys()), parent=parent.name,
                            method='merge', kwargs=kwargs, class_='merge')
        self.gates[name] = new_gate

    def subtraction(self, target: list, parent: str, new_population_name: str) -> None:
        """
        Given a target population and a parent population, generate a new population by subtraction of the
        target population from the parent
        :return: None
        """
        assert parent in self.populations.keys(), 'Error: parent population not recognised'
        assert all([t in self.populations.keys() for t in target]), 'Error: target population not recognised'
        assert new_population_name not in self.populations.keys(), f'Error: a population with name ' \
                                                                   f'{new_population_name} already exists'

        x = self.populations[parent].geom['x']
        y = self.populations[parent].geom['y']
        pindex = self.populations[parent].index
        tindex = np.unique(np.concatenate([self.populations[t].index for t in target], axis=0))
        index = np.setdiff1d(pindex, tindex)
        # index = [p for p in pindex if p not in tindex]
        new_population = ChildPopulationCollection(gate_type='sub')
        new_population.add_population(new_population_name)
        new_population.populations[new_population_name].update_geom(x=x, y=y, shape='sub')
        new_population.populations[new_population_name].update_index(index)
        self.update_populations(output=new_population, parent_df=self.get_population_df(parent),
                                parent_name=parent, warnings=[])
        kwargs = [('parent', parent), ('target', target), ('name', new_population_name),
                  ('x', x), ('y', y), ('child_populations', new_population)]
        new_gate = DataGate(gate_name=f'{parent}_minus_{target}', children=[new_population_name],
                            parent=parent, method='subtraction', kwargs=kwargs, class_='subtraction')
        self.gates[f'{parent}_minus_{target}'] = new_gate

    def create_gate(self, gate_name: str, parent: str, class_: str, method: str, kwargs: dict,
                    child_populations: ChildPopulationCollection) -> bool:
        """
        Define a new gate to be used using 'apply' method
        :param gate_name: Name of the gate
        :param parent: Name of parent population gate is applied to
        :param class_: Name of a valid gating class
        :param method: Name of the class method to invoke upon gating
        :param kwargs: Keyword arguments (include constructor arguments and method arguments)
        :param child_populations: A valid ChildPopulationCollection object describing the resulting populations
        :return: True if successful, else False
        """
        if gate_name in self.gates.keys():
            print(f'Error: gate with name {gate_name} already exists.')
            return False
        if class_ not in self.gating_classes:
            print(f'Error: invalid gate class, must be one of {self.gating_classes}')
            return False
        kwargs['child_populations'] = child_populations
        if not self.__check_class_args(self.gating_classes[class_], method, **kwargs):
            return False
        kwargs = [(k, v) for k, v in kwargs.items()]
        new_gate = DataGate(gate_name=gate_name, children=list(child_populations.populations.keys()), parent=parent,
                            method=method, kwargs=kwargs, class_=class_)
        self.gates[gate_name] = new_gate
        return True

    def __apply_checks(self, gate_name: str) -> DataGate or None:
        """
        Default checks applied whenever a gate is applied
        :param gate_name: Name of gate to apply
        :return: Gate document (None if checks fail)
        """
        if gate_name not in self.gates.keys():
            print(f'Error: {gate_name} does not exist. You must create this gate first using the create_gate method')
            return None
        gatedoc = self.gates[gate_name]
        if gatedoc.parent not in self.populations.keys():
            print('Invalid parent; does not exist in current Gating object')
            return None
        for c in gatedoc.children:
            if c in self.populations.keys():
                print(f'Error: population {c} already exists, if you wish to overwrite this population please remove'
                      f' it with the remove_population method and then try again')
                return None
        return gatedoc

    def __construct_class_and_gate(self, gatedoc: DataGate, kwargs: dict, feedback: bool = True):
        """
        Construct a gating class object and apply the intended method for gating
        :param gatedoc: Gate document generated with `create_gate`
        :param kwargs: keyword arguments for constructor and method
        :return: None
        """
        klass = self.gating_classes[gatedoc.class_]
        parent_population = self.get_population_df(gatedoc.parent)
        expected_const_args = get_params(klass)
        constructor_args = {k: v for k, v in kwargs.items()
                            if k in expected_const_args}
        method_args = {k: v for k, v in kwargs.items()
                       if k in inspect.signature(getattr(klass, gatedoc.method)).parameters.keys()}
        analyst = klass(data=parent_population, **constructor_args)
        output = getattr(analyst, gatedoc.method)(**method_args)
        if feedback:
            print(f'------ {gatedoc.gate_name} ------')
            if analyst.warnings:
                for x in analyst.warnings:
                    print(x)
        self.update_populations(output, parent_df=parent_population,
                                warnings=analyst.warnings, parent_name=gatedoc.parent)
        if feedback:
            for pop in output.populations.keys():
                print(f'New population: {pop}')
                print(f'...proportion of total events: {self.populations[pop].prop_of_total:.3f}')
                print(f'...proportion of parent: {self.populations[pop].prop_of_parent:.3f}')
            print('-----------------------')

    def apply(self, gate_name: str, plot_output: bool = True, feedback: bool = True, **kwargs) -> None:
        """
        Apply a gate to events data (must be generated with `create_gate` first)
        :param gate_name: Name of the gate to apply
        :param plot_output: If True, resulting gates will be printed to screen
        :param feedback: If True, print feedback
        :return: None
        """
        gatedoc = self.__apply_checks(gate_name)
        if gatedoc is None:
            return None
        gkwargs = {k: v for k, v in gatedoc.kwargs}
        # Add kwargs if given
        for k, v in kwargs.items():
            gkwargs[k] = v
        if 'fmo_x' in gkwargs.keys():
            gkwargs['fmo_x'] = self.get_fmo_data(gatedoc.parent, gkwargs['fmo_x'], sml_profiles=None)
        if 'fmo_y' in kwargs.keys():
            gkwargs['fmo_y'] = self.get_fmo_data(gatedoc.parent, gkwargs['fmo_y'], sml_profiles=None)
        if gatedoc.class_ == 'merge':
            self.merge(population_left=gkwargs.get('left'), population_right=gkwargs.get('right'),
                       new_population_name=gkwargs.get('name'))
        elif gatedoc.class_ == 'subtraction':
            self.subtraction(target=gkwargs.get('target'), parent=gkwargs.get('parent'),
                             new_population_name=gkwargs.get('name'))
        else:
            self.__construct_class_and_gate(gatedoc, gkwargs, feedback)
        if plot_output:
            self.plotting.plot_gate(gate_name=gate_name)

    def update_populations(self, output: ChildPopulationCollection, parent_df: pd.DataFrame, warnings: list,
                           parent_name: str) -> ChildPopulationCollection:
        """
        Given some ChildPopulationCollection object generated from a gate, update saved populations
        :param output: ChildPopulationCollection object generated from a gate
        :param parent_df: pandas dataframe of events data from parent population
        :param warnings: list of warnings generated from gate
        :param parent_name: name of the parent population
        :return: output
        """
        for name, population in output.populations.items():
            n = len(population.index)
            if n == 0:
                prop_of_total = 0
                prop_of_parent = 0
            else:
                prop_of_parent = n / parent_df.shape[0]
                prop_of_total = n / self.data.shape[0]
            geom = None
            if population.geom is not None:
                geom = population.geom.as_dict()
            self.populations[name] = Node(name=name, population_name=name, index=population.index,
                                          prop_of_parent=prop_of_parent,
                                          prop_of_total=prop_of_total,
                                          geom=geom, warnings=warnings,
                                          parent=self.populations[parent_name])
        return output

    def apply_many(self, gates: list = None, apply_all: bool = False,
                   plot_outcome: bool = False, feedback: bool = True) -> None:
        """
        Apply multiple existing gates sequentially
        :param gates: Name of gates to apply (NOTE: Gates must be provided in sequential order!)
        :param apply_all: If True, gates is ignored and all current gates will be applied
        (population tree must be empty)
        :param plot_outcome: If True, resulting gates will be printed to screen
        :param feedback: If True, feedback will be printed to stdout
        :return: None
        """
        if gates is None:
            gates = list()
        if apply_all:
            if len(self.populations.keys()) != 1:
                print('User has chosen to apply all gates on a file with existing populations, '
                      'when using the `apply_all` command files should have no existing populations. '
                      'Remove existing populations from file before continuing. Aborting.')
                return None
            gates_to_apply = self.gates.keys()
        else:
            if any([x not in self.gates.keys() for x in gates]):
                print(f'Error: some gate names provided appear invalid; valid gates: {self.gates.keys()}')
                return None
            gates_to_apply = [name for name, _ in self.gates.items() if name in gates]
        for gate_name in gates_to_apply:
            if feedback:
                print(f'Applying {gate_name}...')
            self.apply(gate_name, plot_output=plot_outcome, feedback=feedback)
        if feedback:
            print('Complete!')

    def __update_index(self, population_name: str, geom: dict):
        """
        Given some new gating geom and the name of a population to update, update the population index
        :param population_name: name of population to update
        :param geom: valid dictionary describing geom
        :return: None
        """
        assert population_name in self.populations.keys(), f'Population {population_name} does not exist'
        parent_name = self.populations[population_name].parent.name
        parent = self.get_population_df(parent_name, transform=False)
        transform_x, transform_y = geom.get('transform_x'), geom.get('transform_y')
        x, y = geom.get('x'), geom.get('y')
        assert x, 'Geom is missing value for "x"'
        if transform_x is not None and x is not None:
            parent = apply_transform(parent, transform_method=transform_x,
                                     features_to_transform=[x])
        if transform_y is not None and y is not None:
            parent = apply_transform(parent, transform_method=transform_y,
                                     features_to_transform=[y])

        if geom['shape'] == 'threshold':
            assert 'threshold' in geom.keys(), 'Geom must contain a key "threshold" with a float value'
            assert 'transform_x' in geom.keys(), 'Geom must contain a key "transform_x", ' \
                                                 'the transform method for the x-axis'
            assert 'definition' in geom.keys(), 'Geom must contain key "definition", a string value that indicates ' \
                                                'if population is the "positive" or "negative"'
            if geom['definition'] == '+':
                return parent[parent[geom['x']] >= geom['threshold']].index.values
            if geom['definition'] == '-':
                return parent[parent[geom['x']] < geom['threshold']].index.values
            raise ValueError('Definition must have a value of "+" or "-" for a 1D threshold gate')

        if geom['shape'] == '2d_threshold':
            def geom_bool(definition, p):
                p = p.round(decimals=2)
                x_, y_ = geom['x'], geom['y']
                tx, ty = round(geom['threshold_x'], 2), round(geom['threshold_y'], 2)
                if definition == '++':
                    return p[(p[x_] > tx) & (p[y_] > ty)].index.values
                if definition == '--':
                    return p[(p[x_] < tx) & (p[y_] < ty)].index.values
                if definition == '+-':
                    return p[(p[x_] > tx) & (p[y_] < ty)].index.values
                if definition == '-+':
                    return p[(p[x_] < tx) & (p[y_] > ty)].index.values
                raise ValueError('Definition must have a value of "+-", "-+", "--", or "++" for a 2D threshold gate')

            assert 'definition' in geom.keys(), 'Geom must contain key "definition", a string value that indicates ' \
                                                'if population is the "positive" or "negative"'
            assert all([t in geom.keys() for t in ['threshold_x', 'threshold_y']]), \
                'Geom must contain keys "threshold_x" and "threshold_y" both with a float value'
            assert y, 'Geom is missing value for "y"'
            assert all([t in geom.keys() for t in ['transform_x', 'transform_y']]), 'Geom must contain a key "transform_x", ' \
                                                                                    'the transform method for the x-axis AND ' \
                                                                                    'a key "transform_y", ' \
                                                                                    'the transform method for the y-axis'

            if type(geom['definition']) == list:
                idx = list(map(lambda d: geom_bool(d, parent), geom['definition']))
                return [i for l in idx for i in l]
            else:
                return geom_bool(geom['definition'], parent)

        if geom['shape'] == 'rect':
            keys = ['x_min', 'x_max', 'y_min', 'y_max']
            assert 'definition' in geom.keys(), 'Geom must contain key "definition", a string value that indicates ' \
                                                'if population is the "positive" or "negative"'
            assert all([r in geom.keys() for r in keys]), f'Geom must contain keys {keys} both with a float value'
            assert all([t in geom.keys() for t in ['transform_x', 'transform_y']]), 'Geom must contain a key "transform_x", ' \
                                                                                    'the transform method for the x-axis AND ' \
                                                                                    'a key "transform_y", ' \
                                                                                    'the transform method for the y-axis'
            assert y, 'Geom is missing value for "y"'

            x = (parent[geom['x']] >= geom['x_min']) & (parent[geom['x']] <= geom['x_max'])
            y = (parent[geom['y']] >= geom['y_min']) & (parent[geom['y']] <= geom['y_max'])
            pos = parent[x & y]
            if geom['definition'] == '+':
                return pos.index.values
            if geom['definition'] == '-':
                return parent[~parent.index.isin(pos.index)].index.values
            raise ValueError('Definition must have a value of "+" or "-" for a rectangular geom')

        if geom['shape'] == 'ellipse':
            keys = ['centroid', 'width', 'height', 'angle']
            assert 'definition' in geom.keys(), 'Geom must contain key "definition", a string value that indicates ' \
                                                'if population is the "positive" or "negative"'
            assert all([c in geom.keys() for c in keys]), f'Geom must contain keys {keys}; note, centroid must be a tuple and all others a float value'
            assert y, 'Geom is missing value for "y"'
            assert all([t in geom.keys() for t in ['transform_x', 'transform_y']]), 'Geom must contain a key "transform_x", ' \
                                                                                    'the transform method for the x-axis AND ' \
                                                                                    'a key "transform_y", ' \
                                                                                    'the transform method for the y-axis'

            channels = [geom['x'], geom['y']]
            mask = inside_ellipse(parent[channels].values,
                                  center=tuple(geom['centroid']),
                                  width=geom['width'],
                                  height=geom['height'],
                                  angle=geom['angle'])
            pos = parent[mask]
            if geom['definition'] == '+':
                return pos.index
            if geom['definition'] == '-':
                return parent[~parent.index.isin(pos.index)].index.values
            raise ValueError('Definition must have a value of "+" or "-" for a ellipse geom')

        if geom['shape'] == 'poly':
            keys = ['cords', 'transform_x', 'transform_y', 'x', 'y']
            assert all([c in geom.keys() for c in keys]), f'Geom must contain keys {keys}'
            assert type(geom.get('cords')) == dict, 'Cords should be of type dictionary with keys: x, y'
            cords = geom.get('cords')
            assert all([_ in cords.keys() for _ in ['x', 'y']]), 'Cords should contain keys: x, y'

            poly = Polygon([(x, y) for x, y in zip(cords['x'], cords['y'])])
            pos = inside_polygon(parent, x, y, poly)
            return pos.index
        raise ValueError('Geom shape not recognised, should be one of: threshold, 2d_threshold, ellipse, rect, poly')

    def edit_gate(self, gate_name: str, updated_geom: dict, delete: bool = True):
        """
        Manually replace the outcome of a gate by updating the geom of it's child populations.
        :param gate_name: name of gate to update
        :param updated_geom: new geom as valid dictionary
        :param delete: if True, all populations downstream of immediate children will be removed. This is recommended
        as edit_gate does not update the index of populations downstream of the immediate children.
        :return: None
        """
        print(f'Editing gate: {gate_name}')
        assert gate_name in self.gates.keys(), f'Invalid gate, existing gates are: {self.gates.keys()}'
        children = self.gates[gate_name].children
        effected_populations = list()
        immediate_children = list()
        for c in children:
            print(f'Updating {c}')
            assert c in updated_geom.keys(), f'Invalid child populations specified/missing child, gate {gate_name} ' \
                                             f'has the following children: {children}'
            self.populations[c].geom = updated_geom[c]
            self.populations[c].index = self.__update_index(c, updated_geom[c])
            effected_populations = effected_populations + self.find_dependencies(population=c)
            immediate_children = immediate_children + [n.name for n in self.populations[c].children]
        effected_gates = [name for name, gate in self.gates.items() if gate.parent in effected_populations]
        print(f'The following gates are downstream of {gate_name} and will need to be applied again: {effected_gates}')
        if delete:
            for c in immediate_children:
                self.remove_population(c)
        print('Edit complete!')

    def nudge_threshold(self, gate_name: str, new_x: float, new_y: float or None = None):
        assert gate_name in self.gates.keys(), 'Invalid gate name'
        assert self.gates[gate_name].class_ == 'DensityThreshold', 'Can only nudge threshold gates'
        children = self.gates[gate_name].children
        geoms = {c: self.fetch_geom(c) for c in children}
        for c in children:
            if self.gates[gate_name].method == 'gate_1d':
                geoms[c]['threshold'] = new_x
            else:
                geoms[c]['threshold_x'] = new_x
                if new_y is not None:
                    geoms[c]['threshold_y'] = new_y
        self.edit_gate(gate_name, updated_geom=geoms)

    def find_dependencies(self, population: str = None) -> list or None:
        """
        For a given population find all dependencies
        :param population: population name
        :return: List of populations dependent on given population
        """
        if population not in self.populations.keys():
            print(f'Error: population {population} does not exist; '
                  f'valid population names include: {self.populations.keys()}')
            return None
        root = self.populations['root']
        node = self.populations[population]
        return [x.name for x in findall(root, filter_=lambda n: node in n.path)]

    def remove_population(self, population_name: str, hard_delete: bool = False) -> None:
        """
        Remove a population
        :param population_name: name of population to remove
        :param hard_delete: if True, population and dependencies will be removed from database
        :return: None
        """
        if population_name not in self.populations.keys():
            print(f'{population_name} does not exist')
            return None
        downstream_populations = self.find_dependencies(population=population_name)
        self.populations[population_name].parent = None
        for x in downstream_populations:
            self.populations.pop(x)
        if hard_delete:
            self.filegroup.delete_populations(downstream_populations)
            self.filegroup = self.filegroup.save()

    def remove_gate(self, gate_name: str, propagate: bool = True) -> list and list or None:
        """
        Remove gate
        :param gate_name: name of gate to remove
        :param propagate: If True, downstream gates and effected populations will also be removed
        :return: list of removed gates, list of removed populations
        """
        if gate_name not in self.gates.keys():
            print('Error: invalid gate name')
            return None
        gate = self.gates[gate_name]
        if not gate.children or not propagate:
            self.gates.pop(gate_name)
            return True
        # Remove affected gates and downstream populations
        effected_populations = []
        for child in gate.children:
            dependencies = self.find_dependencies(population=child)
            if dependencies is None:
                continue
            effected_populations = effected_populations + dependencies
            self.remove_population(child)
            effected_populations.append(child)
        effected_gates = [name for name, gate in self.gates.items() if gate.parent in effected_populations]
        effected_gates.append(gate_name)
        for g in effected_gates:
            self.gates.pop(g)
        return effected_gates, effected_populations

    def print_population_tree(self, image: bool = False, image_name: str or None = None) -> None:
        """
        Generate a tree diagram of the populations associated to this Gating object and print to stdout
        :param image: if True, an image will be saved to the working directory
        :param image_name: name of the resulting image file, ignored if image = False (optional; default name is of
        format `filename_populations.png`
        :return: None
        """
        root = self.populations['root']
        if image:
            if image_name is None:
                image_name = f'{self.id}_population_tree.png'
            DotExporter(root).to_picture(image_name)
        for pre, fill, node in RenderTree(root):
            print('%s%s' % (pre, node.name))

    def _population_to_mongo(self, population_name: str) -> Population:
        """
        Convert a population into a mongoengine Population document
        :param population_name: Name of population to convert
        :return: Population document
        """
        pop_node = self.populations[population_name]
        if pop_node.geom is None:
            geom = []
        else:
            geom = [(k, v) for k, v in pop_node.geom.items()]

        parent = None
        if pop_node.parent:
            parent = pop_node.parent.name
        pop_mongo = Population(population_name=pop_node.name,
                               parent=parent,
                               prop_of_parent=pop_node.prop_of_parent,
                               prop_of_total=pop_node.prop_of_total,
                               warnings=pop_node.warnings,
                               geom=geom,
                               n=len(pop_node.index))
        pop_mongo.save_index(pop_node.index)
        return pop_mongo

    def save(self, overwrite: bool = False, feedback: bool = True) -> bool:
        """
        Save all gates and population's to mongoDB
        :param overwrite: If True, existing populations/gates for sample will be overwritten
        :return: True if successful else False
        """
        existing_pops = list(self.filegroup.list_populations())
        existing_gates = list(self.filegroup.list_gates())

        # Update populations
        populations_to_save = list()
        for name in self.populations.keys():
            if name in existing_pops:
                existing_population = FileGroup.objects(id=self.mongo_id).get().get_population(name)
                if np.array_equal(existing_population.load_index(), self.populations.get(name).index):
                    populations_to_save.append(existing_population)
                    continue
                if not overwrite:
                    raise ValueError(f'The index for population {name} has been changed, change "overwrite" to '
                                     f'True to overwrite existing data; note this will delete any clusters '
                                     f'currently associated to this population')
                else:
                    if existing_population.clustering:
                        print(f'Warning: index for {name} has changed and the associated clusters '
                              f'are now invalid')
                    populations_to_save.append(self._population_to_mongo(name))
            else:
                populations_to_save.append(self._population_to_mongo(name))
        self.filegroup.populations = populations_to_save

        # Update gates
        self.filegroup.gates = [self._serailise_gate(gate) for gate in self.gates.values()]
        self.filegroup = self.filegroup.save()
        if feedback:
            print('Saved successfully!')
        return True

    def _cluster_idx(self, cluster_id: str, clustering_root: str, meta: bool = True):
        """
        Fetch the index of a given cluster/meta-cluster in associated sample
        :param cluster_id: name of cluster if interest
        :param clustering_root: name of root population for cluster of interest
        :param meta: if True, search for a meta-cluster if False, treat cluster_id as unique clustering ID
        :return: numpy array for index of events contained in cluster
        """
        assert clustering_root in self.populations.keys(), f'Invalid root name, must be one of {self.populations.keys()}'
        fg = FileGroup.objects(id=self.mongo_id).get()
        croot_pop = [p for p in fg.populations if p.population_name == clustering_root][0]
        _, idx = croot_pop.get_cluster(cluster_id=cluster_id, meta=meta)
        return idx

    def register_as_invalid(self):
        fg = FileGroup.objects(id=self.mongo_id).get()
        if fg.flags:
            fg.flags = fg.flags + ',invalid'
        else:
            fg.flags = 'invalid'
        fg.save()


class Template(Gating):
    """
    Generate a reusable template for gating. Inherits all functionality of Gating class.

    Methods:
        save_new_template - save all gates as a GatingStrategy document to database for later use
        load_template - retrieve gates from a GatingStrategy and populate Template object
    """
    def save_new_template(self, template_name: str, overwrite: bool = True) -> bool:
        """
        Save template structure as a GatingStrategy
        :param template_name: name of the template
        :param overwrite: If True, any existing template with the same name will be overwritten
        :return: True if successful, else False
        """
        gating_strategy = GatingStrategy.objects(template_name=template_name)
        if gating_strategy:
            if not overwrite:
                print(f'Template with name {template_name} already exists, set parameter '
                      f'`overwrite` to True to continue')
                return False
            print(f'Overwriting existing gating template {template_name}')
            gating_strategy = gating_strategy[0]
            gating_strategy.gates = [self._serailise_gate(gate) for gate in list(self.gates.values())]
            gating_strategy.last_edit = datetime.now()
            gating_strategy.save()
            templates = [x for x in self.experiment.gating_templates
                         if x.template_name != gating_strategy.template_name]
            templates.append(gating_strategy)
            self.experiment.gating_templates = templates
            self.experiment.save()
            return True
        else:
            print(f'No existing template named {template_name}, creating new template')
            gating_strategy = GatingStrategy()
            gating_strategy.template_name = template_name
            gating_strategy.creation_date = datetime.now()
            gating_strategy.last_edit = datetime.now()
            gating_strategy.gates = [self._serailise_gate(gate) for gate in list(self.gates.values())]
            gating_strategy.save()
            self.experiment.gating_templates.append(gating_strategy)
            self.experiment.save()
            return True

    def load_template(self, template_name: str) -> bool:
        """
        Load gates from a template GatingStrategy
        :param template_name: name of template to load
        :return: True if successful, else False
        """
        gating_strategy = GatingStrategy.objects(template_name=template_name)
        if gating_strategy:
            for gate in gating_strategy[0].gates:
                self._deserialise_gate(gate)
            return True
        else:
            print(f'No template with name {template_name}')
            return False