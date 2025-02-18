import sys
import uuid
from typing import Union, Iterable, List, Callable, Optional, Tuple, Dict, Any, Set
import itertools
import os
from functools import wraps
from dataclasses import dataclass, fields, asdict
from copy import deepcopy
import logging
import collections
import time
from datetime import datetime, timedelta
import tarfile
import re
import shutil

import networkx as nx
import pandas as pd

from pydock3.util import (
    T,
    P,
    filter_kwargs_for_callable,
    Script,
    CleanExit,
    get_hexdigest_of_persistent_md5_hash_of_tuple,
    system_call,
)
from pydock3.dockopt.util import WORKING_DIR_NAME, RETRODOCK_JOBS_DIR_NAME, RESULTS_CSV_FILE_NAME, BEST_RETRODOCK_JOBS_DIR_NAME
from pydock3.config import (
    Parameter,
    flatten_and_parameter_cast_param_dict,
    get_sorted_univalued_flat_parameter_cast_param_dicts_from_multivalued_param_dict,
)
from pydock3.blastermaster.blastermaster import BlasterFiles, get_blaster_steps
from pydock3.dockopt.config import DockoptParametersConfiguration
from pydock3.files import (
    INDOCK_FILE_NAME,
    Dir,
    File,
    OutdockFile,
)
from pydock3.blastermaster.util import (
    BLASTER_FILE_IDENTIFIER_TO_PROPER_BLASTER_FILE_NAME_DICT,
    DOCK_FILE_IDENTIFIERS,
    WorkingDir,
    BlasterFile,
    BlasterStep,
)
from pydock3.jobs import ArrayDockingJob
from pydock3.job_schedulers import SlurmJobScheduler, SGEJobScheduler
from pydock3.dockopt import __file__ as DOCKOPT_INIT_FILE_PATH
from pydock3.retrodock.retrodock import log_job_submission_result, get_results_dataframe_from_actives_job_and_decoys_job_outdock_files, sort_by_energy_and_drop_duplicate_molecules
from pydock3.blastermaster.util import DEFAULT_FILES_DIR_PATH
from pydock3.dockopt.results import DockoptStepResultsManager, DockoptStepSequenceIterationResultsManager, DockoptStepSequenceResultsManager
from pydock3.criterion.enrichment.logauc import NormalizedLogAUC
from pydock3.dockopt.pipeline import PipelineComponent, PipelineComponentSequence, PipelineComponentSequenceIteration, Pipeline
from pydock3.dockopt.parameters import DockoptComponentParametersManager
from pydock3.dockopt.docking_configuration import DockingConfiguration, DockFileCoordinates, DockFileCoordinate, IndockFileCoordinate
from pydock3.dockopt.dock_files_modification.matching_spheres_perturbation import MatchingSpheresPerturbationStep
from pydock3.retrodock.retrospective_dataset import RetrospectiveDataset

#
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


#
SCHEDULER_NAME_TO_CLASS_DICT = {
    "sge": SGEJobScheduler,
    "slurm": SlurmJobScheduler,
}

#
CRITERION_CLASS_DICT = {"normalized_log_auc": NormalizedLogAUC}

#
MIN_SECONDS_BETWEEN_QUEUE_CHECKS = 2
MIN_SECONDS_BETWEEN_TASK_OUTPUT_DETECTION_REATTEMPTS = 30
MIN_SECONDS_BETWEEN_TASK_OUTPUT_LOADING_REATTEMPTS = 30


@dataclass
class DockoptPipelineComponentRunFuncArgSet:  # TODO: rename?
    scheduler: str
    temp_storage_path: Optional[str] = None
    retrodock_job_max_reattempts: int = 0
    allow_failed_retrodock_jobs: bool = False
    retrodock_job_timeout_minutes: Optional[int] = None
    max_task_array_size: Optional[int] = None
    extra_submission_cmd_params_str: Optional[str] = None
    sleep_seconds_after_copying_output: int = 0
    export_decoys_mol2: bool = False
    delete_intermediate_files: bool = False
    max_scheduler_jobs_running_at_a_time: Optional[int] = None


class Dockopt(Script):
    JOB_DIR_NAME = "dockopt_job"
    CONFIG_FILE_NAME = "dockopt_config.yaml"
    ACTIVES_TGZ_FILE_NAME = "actives.tgz"
    DECOYS_TGZ_FILE_NAME = "decoys.tgz"
    DEFAULT_CONFIG_FILE_PATH = os.path.join(
        os.path.dirname(DOCKOPT_INIT_FILE_PATH), "default_dockopt_config.yaml"
    )

    def __init__(self) -> None:
        super().__init__()

    @staticmethod
    def handle_run_func(run_func: Callable[P, T]) -> Callable[P, T]:
        """Decorator for run functions to handle common functionality."""

        @wraps(run_func)
        def wrapper(self, *args: P.args, **kwargs: P.kwargs):
            with CleanExit():
                logger.info(f"Running {self.__class__.__name__}")
                run_func(self, *args, **kwargs)

        return wrapper

    def new(
        self,
        job_dir_path: str = JOB_DIR_NAME,
    ) -> None:
        """Set up a new Dockopt job directory."""

        # check if job dir already exists
        if os.path.exists(job_dir_path):
            logger.info(f"Job directory `{job_dir_path}` already exists. Exiting.")
            return

        # create job dir
        job_dir = Dir(path=job_dir_path, create=True, reset=False)

        # create working dir & copy in blaster files
        blaster_file_names = list(BLASTER_FILE_IDENTIFIER_TO_PROPER_BLASTER_FILE_NAME_DICT.values())
        user_provided_blaster_file_paths = [
            os.path.abspath(f) for f in blaster_file_names if os.path.isfile(f)
        ]
        files_to_copy_str = "\n\t".join(user_provided_blaster_file_paths)
        if user_provided_blaster_file_paths:
            logger.info(
                f"Copying the following files from current directory into job working directory:\n\t{files_to_copy_str}"
            )
            for blaster_file_path in user_provided_blaster_file_paths:
                job_dir.copy_in_file(blaster_file_path)
        else:
            logger.info(
                f"No blaster files detected in current working directory. Be sure to add them manually before running the job."
            )

        # copy in actives and decoys TGZ files
        tgz_files = [self.ACTIVES_TGZ_FILE_NAME, self.DECOYS_TGZ_FILE_NAME]
        tgz_file_names_in_cwd = [f for f in tgz_files if os.path.isfile(f)]
        tgz_file_names_not_in_cwd = [f for f in tgz_files if not os.path.isfile(f)]
        if tgz_file_names_in_cwd:
            files_to_copy_str = "\n\t".join(tgz_file_names_in_cwd)
            logger.info(
                f"Copying the following files from current directory into job directory:\n\t{files_to_copy_str}"
            )
            for tgz_file_name in tgz_file_names_in_cwd:
                job_dir.copy_in_file(tgz_file_name)
        if tgz_file_names_not_in_cwd:
            files_missing_str = "\n\t".join(tgz_file_names_not_in_cwd)
            logger.info(
                f"The following required files were not found in current working directory. Be sure to add them manually to the job directory before running the job.\n\t{files_missing_str}"
            )

        # write fresh config file from default file
        save_path = os.path.join(job_dir.path, self.CONFIG_FILE_NAME)
        DockoptParametersConfiguration.write_config_file(
            save_path, self.DEFAULT_CONFIG_FILE_PATH
        )

    @handle_run_func.__get__(0)
    def run(
        self,
        scheduler: str,
        job_dir_path: str = ".",
        config_file_path: Optional[str] = None,
        actives_tgz_file_path: Optional[str] = None,
        decoys_tgz_file_path: Optional[str] = None,
        retrodock_job_max_reattempts: int = 0,
        allow_failed_retrodock_jobs: bool = False,
        retrodock_job_timeout_minutes: Optional[str] = None,
        max_task_array_size: Optional[int] = None,
        extra_submission_cmd_params_str: Optional[str] = None,
        sleep_seconds_after_copying_output: int = 0,
        export_decoys_mol2: bool = False,
        delete_intermediate_files: bool = False,
        #max_scheduler_jobs_running_at_a_time: Optional[str] = None,  # TODO
        force_redock: bool = False,
        force_rewrite_results: bool = False,
        force_rewrite_report: bool = False,
    ) -> None:
        """Run DockOpt job."""

        #
        job_dir_path = os.path.abspath(job_dir_path)
        logger.info(f"Running DockOpt job in directory: {job_dir_path}")

        # validate args
        if config_file_path is None:
            config_file_path = os.path.join(job_dir_path, self.CONFIG_FILE_NAME)
        if actives_tgz_file_path is None:
            actives_tgz_file_path = os.path.join(job_dir_path, self.ACTIVES_TGZ_FILE_NAME)
        if decoys_tgz_file_path is None:
            decoys_tgz_file_path = os.path.join(job_dir_path, self.DECOYS_TGZ_FILE_NAME)
        try:
            File.validate_file_exists(config_file_path)
        except FileNotFoundError:
            logger.error("Config file not found. Are you in the job directory?")
            return
        try:
            File.validate_file_exists(actives_tgz_file_path)
            File.validate_file_exists(decoys_tgz_file_path)
        except FileNotFoundError:
            logger.error(
                "Actives TGZ file and/or decoys TGZ file not found. Did you put them in the job directory?\nNote: if you do not have actives and decoys, please use blastermaster instead of dockopt."
            )
            return
        if scheduler not in SCHEDULER_NAME_TO_CLASS_DICT:
            logger.error(
                f"scheduler flag must be one of: {list(SCHEDULER_NAME_TO_CLASS_DICT.keys())}"
            )
            return

        #
        try:
            scheduler = SCHEDULER_NAME_TO_CLASS_DICT[scheduler]()
        except KeyError:
            logger.error(
                f"The following environmental variables are required to use the {scheduler} job scheduler: {SCHEDULER_NAME_TO_CLASS_DICT[scheduler].REQUIRED_ENV_VAR_NAMES}"
            )
            return

        #
        try:
            temp_storage_path = os.environ["TMPDIR"]
        except KeyError:
            logger.error(
                "The following environmental variables are required to submit retrodock jobs: TMPDIR"
            )
            return

        #
        retrospective_dataset = RetrospectiveDataset(actives_tgz_file_path, decoys_tgz_file_path, 'actives', 'decoys')

        #
        component_run_func_arg_set = DockoptPipelineComponentRunFuncArgSet(
            scheduler=scheduler,
            temp_storage_path=temp_storage_path,
            retrodock_job_max_reattempts=retrodock_job_max_reattempts,
            allow_failed_retrodock_jobs=allow_failed_retrodock_jobs,
            retrodock_job_timeout_minutes=retrodock_job_timeout_minutes,
            max_task_array_size=max_task_array_size,
            extra_submission_cmd_params_str=extra_submission_cmd_params_str,
            sleep_seconds_after_copying_output=sleep_seconds_after_copying_output,
            export_decoys_mol2=export_decoys_mol2,
            delete_intermediate_files=delete_intermediate_files,
            #max_scheduler_jobs_running_at_a_time=max_scheduler_jobs_running_at_a_time,  # TODO: move checking of this to this class?
        )

        #
        logger.info("Loading config file")
        config = DockoptParametersConfiguration(config_file_path)

        #
        config_params_str = "\n".join(
            [
                f"{param_name}: {param.value}"
                for param_name, param in flatten_and_parameter_cast_param_dict(
                    config.param_dict
                ).items()
            ]
        )
        logger.debug(f"Parameters:\n{config_params_str}")

        #
        proper_blaster_file_names = list(BLASTER_FILE_IDENTIFIER_TO_PROPER_BLASTER_FILE_NAME_DICT.values())
        blaster_files_to_copy_in = [
            os.path.abspath(f) for f in proper_blaster_file_names if os.path.isfile(f)
        ]

        #
        pipeline = DockoptPipeline(
            **config.param_dict["pipeline"],
            pipeline_dir_path=job_dir_path,
            retrospective_dataset=retrospective_dataset,
            blaster_files_to_copy_in=blaster_files_to_copy_in,
        )
        pipeline.run(
            component_run_func_arg_set=component_run_func_arg_set,
            force_redock=force_redock,
            force_rewrite_results=force_rewrite_results,
            force_rewrite_report=force_rewrite_report,
        )


class DockoptStep(PipelineComponent):
    def __init__(
            self,
            pipeline_dir_path: str,
            component_id: str,
            criterion: str,
            top_n: int,
            retrospective_dataset: RetrospectiveDataset,
            parameters: Iterable[dict],
            dock_files_to_use_from_previous_component: dict,
            blaster_files_to_copy_in: Iterable[BlasterFile],
            last_component_completed: Union[PipelineComponent, None] = None,
    ) -> None:
        super().__init__(
            pipeline_dir_path=pipeline_dir_path,
            component_id=component_id,
            criterion=criterion,
            top_n=top_n,
            results_manager=DockoptStepResultsManager(RESULTS_CSV_FILE_NAME),
        )

        #
        self.retrospective_dataset = retrospective_dataset

        #
        blaster_file_names = list(BLASTER_FILE_IDENTIFIER_TO_PROPER_BLASTER_FILE_NAME_DICT.values())
        backup_blaster_file_paths = [
            os.path.join(DEFAULT_FILES_DIR_PATH, blaster_file_name)
            for blaster_file_name in blaster_file_names
        ]
        new_file_names = [
            f"{File.get_file_name_of_file(file_path)}_1"  # TODO
            for file_path in blaster_files_to_copy_in
        ]  # all nodes in graph will be numerically indexed, including input files
        new_backup_file_names = [
            f"{File.get_file_name_of_file(file_path)}_1"  # TODO
            for file_path in backup_blaster_file_paths
        ]  # ^
        # TODO: remove need to copy these blaster files in for every step
        self.working_dir = WorkingDir(
            path=os.path.join(self.component_dir.path, WORKING_DIR_NAME),
            create=True,
            reset=False,
            files_to_copy_in=blaster_files_to_copy_in,
            new_file_names=new_file_names,
            backup_files_to_copy_in=backup_blaster_file_paths,
            new_backup_file_names=new_backup_file_names,
        )
        self.retrodock_jobs_dir = Dir(
            path=os.path.join(self.component_dir.path, RETRODOCK_JOBS_DIR_NAME),
            create=True,
            reset=False,
        )

        #
        self.best_retrodock_jobs_dir = Dir(
            path=os.path.join(self.component_dir.path, BEST_RETRODOCK_JOBS_DIR_NAME),
            create=True,
            reset=False,
        )

        #
        self.retrospective_dataset = retrospective_dataset

        #
        if isinstance(parameters["custom_dock_executable"], list):
            custom_dock_executables = [custom_dock_executable for custom_dock_executable in parameters["custom_dock_executable"]]
        else:
            custom_dock_executables = [parameters["custom_dock_executable"]]

        #
        sorted_dock_files_generation_flat_param_dicts = get_sorted_univalued_flat_parameter_cast_param_dicts_from_multivalued_param_dict(parameters["dock_files_generation"])
        sorted_dock_files_modification_flat_param_dicts = get_sorted_univalued_flat_parameter_cast_param_dicts_from_multivalued_param_dict(parameters["dock_files_modification"])
        sorted_indock_file_generation_flat_param_dicts = get_sorted_univalued_flat_parameter_cast_param_dicts_from_multivalued_param_dict(parameters["indock_file_generation"])

        #
        logger.debug(f"{len(sorted_dock_files_generation_flat_param_dicts)} dock file generation parametrizations:\n{sorted_dock_files_generation_flat_param_dicts}")
        logger.debug(f"{len(sorted_dock_files_modification_flat_param_dicts)} dock file modification parametrizations:\n{sorted_dock_files_modification_flat_param_dicts}")
        logger.debug(f"{len(sorted_indock_file_generation_flat_param_dicts)} indock file generation parametrizations:\n{sorted_indock_file_generation_flat_param_dicts}")

        #
        logger.info("Generating directed acyclic graph of docking configurations")
        graph = nx.DiGraph()

        #
        last_component_docking_configurations = []
        if last_component_completed is not None and any(list(dock_files_to_use_from_previous_component.values())):
            logger.debug(f"Using the following dock files from previous component: {sorted([key for key, value in dock_files_to_use_from_previous_component.items() if value])}")
            for row_index, row in last_component_completed.load_results_dataframe().head(last_component_completed.top_n).iterrows():
                dc = DockingConfiguration.from_dict(row.to_dict())
                for dock_file_identifier, should_be_used in dock_files_to_use_from_previous_component.items():
                    if should_be_used:
                        #
                        dock_file_node_id = getattr(dc.dock_file_coordinates, dock_file_identifier).node_id
                        dock_file_lineage_subgraph = self._get_dock_file_lineage_subgraph(
                            graph=last_component_completed.graph,
                            dock_file_node_id=dock_file_node_id,
                        )
                        graph = nx.compose(graph, dock_file_lineage_subgraph)
                last_component_docking_configurations.append(dc)

        #
        dock_file_identifier_counter_dict = collections.defaultdict(int)
        blaster_file_node_id_to_numerical_suffix_dict = {}
        blaster_files = BlasterFiles(working_dir=self.working_dir)
        partial_dock_file_nodes_combination_dicts = []
        if any([not x for x in dock_files_to_use_from_previous_component.values()]):
            logger.debug(f"The following dock files will be generated during this step: {sorted([key for key, value in dock_files_to_use_from_previous_component.items() if not value])}")
            for dock_files_generation_flat_param_dict in sorted_dock_files_generation_flat_param_dicts:
                # get config for get_blaster_steps
                # each value in dict must be an instance of Parameter
                steps = get_blaster_steps(
                    blaster_files=blaster_files,
                    flat_param_dict=dock_files_generation_flat_param_dict,
                    working_dir=self.working_dir,
                )

                # form subgraph for this dock_files_generation_param_dict from the blaster steps it defines
                subgraph = self._get_graph_from_all_steps_in_order(self.component_id, steps)

                #
                partial_dock_file_nodes_combination_dict = {}
                for dock_file_identifier, should_be_used in dock_files_to_use_from_previous_component.items():
                    step_hash_to_edges_dict = collections.defaultdict(list)
                    step_hash_to_step_class_instance_dict = {}
                    if not should_be_used:  # need to create during this dockopt step, so add to graph
                        #
                        dock_file_node_id = self._get_blaster_file_node_with_blaster_file_identifier(dock_file_identifier, subgraph)
                        partial_dock_file_nodes_combination_dict[dock_file_identifier] = dock_file_node_id
                        dock_file_lineage_subgraph = self._get_dock_file_lineage_subgraph(
                            graph=subgraph,
                            dock_file_node_id=dock_file_node_id,
                        )

                        #
                        new_dock_file_lineage_subgraph = deepcopy(dock_file_lineage_subgraph)
                        for node_id in self._get_blaster_file_nodes(dock_file_lineage_subgraph):
                            if node_id not in blaster_file_node_id_to_numerical_suffix_dict:
                                blaster_file_identifier = dock_file_lineage_subgraph.nodes[node_id]['blaster_file'].identifier
                                blaster_file_node_id_to_numerical_suffix_dict[node_id] = dock_file_identifier_counter_dict[blaster_file_identifier] + 1
                                dock_file_identifier_counter_dict[blaster_file_identifier] += 1
                            new_blaster_file = deepcopy(dock_file_lineage_subgraph.nodes[node_id]['blaster_file'])
                            new_blaster_file.path = f"{new_blaster_file.path}_{blaster_file_node_id_to_numerical_suffix_dict[node_id]}"
                            new_dock_file_lineage_subgraph.nodes[node_id]['blaster_file'] = new_blaster_file
                        dock_file_lineage_subgraph = new_dock_file_lineage_subgraph

                        #
                        for u, v, data in dock_file_lineage_subgraph.edges(data=True):
                            step_hash_to_edges_dict[data["step_hash"]].append((u, v))

                        #
                        for step_hash, edges in step_hash_to_edges_dict.items():
                            #
                            kwargs = {"working_dir": self.working_dir}
                            for (parent_node, child_node) in edges:
                                edge_data_dict = dock_file_lineage_subgraph.get_edge_data(parent_node, child_node)
                                parent_node_data_dict = dock_file_lineage_subgraph.nodes[parent_node]
                                child_node_data_dict = dock_file_lineage_subgraph.nodes[child_node]
                                parent_node_step_var_name = edge_data_dict["parent_node_step_var_name"]
                                child_node_step_var_name = edge_data_dict["child_node_step_var_name"]
                                if "blaster_file" in parent_node_data_dict:
                                    kwargs[parent_node_step_var_name] = parent_node_data_dict[
                                        "blaster_file"
                                    ]
                                if "parameter" in parent_node_data_dict:
                                    kwargs[parent_node_step_var_name] = parent_node_data_dict[
                                        "parameter"
                                    ]
                                if "blaster_file" in child_node_data_dict:
                                    kwargs[child_node_step_var_name] = child_node_data_dict[
                                        "blaster_file"
                                    ]
                                if "parameter" in child_node_data_dict:
                                    kwargs[child_node_step_var_name] = child_node_data_dict["parameter"]

                            #
                            step_class = dock_file_lineage_subgraph.get_edge_data(*edges[0])["step_class"]  # first edge is fine since all edges have same step class
                            step_hash_to_step_class_instance_dict[step_hash] = step_class(**filter_kwargs_for_callable(kwargs, step_class))

                            #
                            for parent_node, child_node in edges:
                                dock_file_lineage_subgraph.get_edge_data(parent_node, child_node)[
                                    "step_instance"
                                ] = step_hash_to_step_class_instance_dict[step_hash]

                        #
                        for u, v, data in dock_file_lineage_subgraph.edges(data=True):
                            u_data = dock_file_lineage_subgraph.nodes[u]
                            v_data = dock_file_lineage_subgraph.nodes[v]

                            #
                            if u_data.get('parameter') is not None:
                                u_node_type = 'parameter'
                            elif u_data.get('blaster_file') is not None:
                                u_node_type = 'blaster_file'
                            else:
                                raise Exception(f"Unrecognized node type for parent `{u}`: {u_data}")

                            #
                            if v_data.get('blaster_file') is not None:
                                v_node_type = 'blaster_file'
                            else:
                                raise Exception(f"Unrecognized node type for child `{v}`: {v_data}")

                            #
                            if graph.has_edge(u, v):
                                for attr in ['parameter', 'blaster_file']:
                                    for n in [u, v]:
                                        if dock_file_lineage_subgraph.nodes[n].get(attr) is not None:
                                            if dock_file_lineage_subgraph.nodes[n].get(attr) != graph.nodes[n].get(attr):
                                                raise Exception(f"`dock_file_lineage_subgraph` and `graph` have nodes with ID `{n}` in common but possess unequal attribute `{attr}`: {dock_file_lineage_subgraph.nodes[n].get(attr)} vs. {graph.nodes[n].get(attr)}")

                            #
                            if graph.has_node(v):
                                for pred in graph.predecessors(v):
                                    if graph.nodes[pred].get(u_node_type) is not None:
                                        if u_data[u_node_type] == graph.nodes[pred][u_node_type]:
                                            continue
                                        if u_node_type == 'parameter':
                                            if u_data[u_node_type].name == graph.nodes[pred][u_node_type].name:
                                                raise Exception(f"Nodes with ID `{v}` ({v_data}) in common in `dock_file_lineage_subgraph` and `graph` have different parent parameter nodes: \n\t{u} {u_data[u_node_type]}\n\t{pred} {graph.nodes[pred][u_node_type]}")
                                        elif u_node_type == 'blaster_file':
                                            if u_data[u_node_type].identifier == graph.nodes[pred][u_node_type].identifier:
                                                raise Exception(f"Nodes with ID `{v}` ({v_data}) in common in `dock_file_lineage_subgraph` and `graph` have different parent blaster_file nodes: \n\t{u} {u_data[u_node_type]}\n\t{pred} {graph.nodes[pred][u_node_type]}")
                                        else:
                                            raise Exception(f"Unrecognized node type for `{u}`: {u_data}")

                        #
                        graph = nx.compose(graph, dock_file_lineage_subgraph)

                #
                partial_dock_file_nodes_combination_dicts.append(partial_dock_file_nodes_combination_dict)

        #
        dc_kwargs_so_far = []
        if last_component_docking_configurations:
            if partial_dock_file_nodes_combination_dicts:
                for last_component_dc, partial_dock_file_nodes_combination_dict in itertools.product(last_component_docking_configurations, partial_dock_file_nodes_combination_dicts):
                    dock_file_coordinates_kwargs = {  # complement + complement = complete
                        **{identifier: DockFileCoordinate(
                            component_id=self.component_id,
                            file_name=graph.nodes[node_id]['blaster_file'].name,
                            node_id=node_id,
                        ) for identifier, node_id in partial_dock_file_nodes_combination_dict.items()},
                        **{identifier: coord for identifier, coord in asdict(last_component_dc.dock_file_coordinates).items() if identifier not in partial_dock_file_nodes_combination_dict},
                    }
                    dock_file_coordinates = DockFileCoordinates(**dock_file_coordinates_kwargs)
                    partial_dc_kwargs = {
                        'dock_file_coordinates': dock_file_coordinates,
                        'dock_files_generation_flat_param_dict': self._get_dock_files_generation_flat_param_dict(graph, dock_file_coordinates),
                    }
                    dc_kwargs_so_far.append(partial_dc_kwargs)
            else:
                for last_component_dc in last_component_docking_configurations:
                    partial_dc_kwargs = {
                        'dock_file_coordinates': deepcopy(last_component_dc.dock_file_coordinates),
                        'dock_files_generation_flat_param_dict': self._get_dock_files_generation_flat_param_dict(graph, last_component_dc.dock_file_coordinates),
                    }
                    dc_kwargs_so_far.append(partial_dc_kwargs)
        else:
            for partial_dock_file_nodes_combination_dict in partial_dock_file_nodes_combination_dicts:
                dock_file_coordinates_kwargs = {
                    **{identifier: DockFileCoordinate(
                        component_id=self.component_id,
                        file_name=graph.nodes[node_id]['blaster_file'].name,
                        node_id=node_id,
                    ) for identifier, node_id in partial_dock_file_nodes_combination_dict.items()},
                }
                dock_file_coordinates = DockFileCoordinates(**dock_file_coordinates_kwargs)
                partial_dc_kwargs = {
                    'dock_file_coordinates': dock_file_coordinates,
                    'dock_files_generation_flat_param_dict': self._get_dock_files_generation_flat_param_dict(graph, dock_file_coordinates),
                }
                dc_kwargs_so_far.append(partial_dc_kwargs)
        logger.debug(f"Number of partial docking configurations after dock files generation specification: {len(dc_kwargs_so_far)}")

        #
        dc_kwargs_so_far = self._get_unique_partial_docking_configuration_kwargs_sorted(dc_kwargs_so_far)
        logger.debug(f"Number of unique partial docking configurations after dock files generation specification: {len(dc_kwargs_so_far)}")

        # matching spheres perturbation
        sorted_unique_matching_spheres_file_nodes = sorted(list(set([partial_dc_kwargs['dock_file_coordinates'].matching_spheres_file.node_id for partial_dc_kwargs in dc_kwargs_so_far])))
        new_dc_kwargs_so_far = []
        num_files_perturbed_so_far = 0
        for dock_files_modification_flat_param_dict in sorted_dock_files_modification_flat_param_dicts:
            if dock_files_modification_flat_param_dict[
                "matching_spheres_perturbation.use"
            ].value:
                #
                matching_spheres_node_to_perturbed_nodes_dict = collections.defaultdict(list)
                for i in range(
                    int(
                        dock_files_modification_flat_param_dict[
                            "matching_spheres_perturbation.num_samples_per_matching_spheres_file"
                        ].value
                    )
                ):
                    for matching_spheres_file_node in sorted_unique_matching_spheres_file_nodes:
                        #
                        matching_spheres_blaster_file = graph.nodes[matching_spheres_file_node]['blaster_file']
                        max_deviation_angstroms = float(
                            dock_files_modification_flat_param_dict[
                                "matching_spheres_perturbation.max_deviation_angstroms"
                            ].value
                        )
                        max_deviation_angstroms_parameter = Parameter(
                            "matching_spheres_perturbation.max_deviation_angstroms",
                            max_deviation_angstroms,
                        )
                        perturbed_matching_spheres_file_path = os.path.join(
                            self.working_dir.path,
                            f"{BLASTER_FILE_IDENTIFIER_TO_PROPER_BLASTER_FILE_NAME_DICT[matching_spheres_blaster_file.identifier]}_p{num_files_perturbed_so_far+1}"  # 'p' for perturbed
                        )
                        perturbed_matching_spheres_file = BlasterFile(perturbed_matching_spheres_file_path, identifier="matching_spheres_file")
                        step = MatchingSpheresPerturbationStep(
                            self.working_dir,
                            matching_spheres_infile=matching_spheres_blaster_file,
                            perturbed_matching_spheres_outfile=perturbed_matching_spheres_file,
                            max_deviation_angstroms_parameter=max_deviation_angstroms_parameter,
                        )
                        num_files_perturbed_so_far += 1

                        #
                        infile_hashes = [matching_spheres_file_node]
                        infiles_hash = get_hexdigest_of_persistent_md5_hash_of_tuple(tuple(sorted(infile_hashes)))

                        # get step hash from infiles hash, step, parameters, and outfiles
                        step_hash = DockoptStep._get_step_hash(self.component_id, step, infiles_hash)

                        # add outfile node
                        outfile, = list(step.outfiles._asdict().values())
                        outfile_hash = DockoptStep._get_outfile_hash(self.component_id, outfile, step_hash)
                        graph.add_node(
                            outfile_hash,
                            blaster_file=deepcopy(outfile.original_file_in_working_dir),
                        )

                        # add parameter node
                        parameter, = list(step.parameters._asdict().values())
                        graph.add_node(parameter.hexdigest_of_persistent_md5_hash, parameter=deepcopy(parameter))

                        # connect each infile node to outfile node
                        infile_step_var_name, = list(step.infiles._asdict().keys())
                        outfile_step_var_name, = list(step.outfiles._asdict().keys())
                        parameter_step_var_name, = list(step.parameters._asdict().keys())
                        graph.add_edge(
                            matching_spheres_file_node,
                            outfile_hash,
                            step_class=step.__class__,
                            original_step_dir_name=step.step_dir.name,
                            step_instance=deepcopy(step),
                            step_hash=step_hash,
                            parent_node_step_var_name=infile_step_var_name,
                            child_node_step_var_name=outfile_step_var_name,
                        )

                        # connect each parameter node to outfile node
                        graph.add_edge(
                            parameter.hexdigest_of_persistent_md5_hash,
                            outfile_hash,
                            step_class=step.__class__,
                            original_step_dir_name=step.step_dir.name,
                            step_instance=deepcopy(step),
                            step_hash=step_hash,
                            parent_node_step_var_name=parameter_step_var_name,
                            child_node_step_var_name=outfile_step_var_name,
                        )

                        #
                        matching_spheres_node_to_perturbed_nodes_dict[matching_spheres_file_node].append(outfile_hash)

                #
                for partial_dc_kwargs in dc_kwargs_so_far:
                    dock_file_coordinates = partial_dc_kwargs['dock_file_coordinates']
                    for perturbed_file_node_id in matching_spheres_node_to_perturbed_nodes_dict[dock_file_coordinates.matching_spheres_file.node_id]:
                        new_partial_dc_kwargs = deepcopy(partial_dc_kwargs)
                        new_partial_dc_kwargs['dock_file_coordinates'].matching_spheres_file = DockFileCoordinate(
                            component_id=self.component_id,
                            file_name=graph.nodes[perturbed_file_node_id]['blaster_file'].name,
                            node_id=perturbed_file_node_id,
                        )
                        new_partial_dc_kwargs['dock_files_modification_flat_param_dict'] = dock_files_modification_flat_param_dict
                        new_dc_kwargs_so_far.append(new_partial_dc_kwargs)
            else:
                temp_dc_kwargs_so_far = deepcopy(dc_kwargs_so_far)
                for partial_dc_kwargs in temp_dc_kwargs_so_far:
                    partial_dc_kwargs['dock_files_modification_flat_param_dict'] = dock_files_modification_flat_param_dict
                new_dc_kwargs_so_far += temp_dc_kwargs_so_far
        logger.debug(f"Number of partial docking configurations after dock files modification specification: {len(new_dc_kwargs_so_far)}")

        #
        dc_kwargs_so_far = self._get_unique_partial_docking_configuration_kwargs_sorted(new_dc_kwargs_so_far)
        logger.debug(f"Number of unique partial docking configurations after dock files modification specification: {len(dc_kwargs_so_far)}")

        #
        new_dc_kwargs_so_far = []
        for i, (partial_dc_kwargs, custom_dock_executable, indock_file_generation_flat_param_dict) in enumerate(itertools.product(dc_kwargs_so_far, custom_dock_executables, sorted_indock_file_generation_flat_param_dicts)):
            configuration_num = i + 1
            new_partial_dc_kwargs = deepcopy(partial_dc_kwargs)
            new_partial_dc_kwargs = {
                'component_id': self.component_id,
                'configuration_num': configuration_num,
                'custom_dock_executable': custom_dock_executable,
                'dock_files_generation_flat_param_dict': partial_dc_kwargs['dock_files_generation_flat_param_dict'],
                'dock_files_modification_flat_param_dict': partial_dc_kwargs['dock_files_modification_flat_param_dict'],
                'indock_file_generation_flat_param_dict': indock_file_generation_flat_param_dict,
                'dock_file_coordinates': partial_dc_kwargs['dock_file_coordinates'],
                'indock_file_coordinate': IndockFileCoordinate(
                    component_id=self.component_id,
                    file_name=f"{INDOCK_FILE_NAME}_{configuration_num}",
                ),
            }
            new_dc_kwargs_so_far.append(new_partial_dc_kwargs)
        logger.debug(f"Number of partial docking configurations after indock file generation specification: {len(new_partial_dc_kwargs)}")

        #
        all_dc_kwargs = self._get_unique_partial_docking_configuration_kwargs_sorted(new_dc_kwargs_so_far)
        logger.debug(f"Number of unique partial docking configurations after indock file generation specification: {len(all_dc_kwargs)}")

        #
        self.docking_configurations = sorted([DockingConfiguration(**dc_kwargs) for dc_kwargs in all_dc_kwargs], key=lambda dc: getattr(dc, 'configuration_num'))

        #
        if last_component_completed is not None:
            self.num_total_docking_configurations_thus_far = len(self.docking_configurations) + last_component_completed.num_total_docking_configurations_thus_far
        else:
            self.num_total_docking_configurations_thus_far = len(self.docking_configurations)

        # validate that there are no cycles (i.e. that it is a directed acyclic graph)
        if not nx.is_directed_acyclic_graph(graph):
            raise Exception("Cycle found in graph!")

        #
        self.graph = graph

        #
        logger.info(f"Number of unique docking configurations: {len(self.docking_configurations)}")

    def _get_unique_partial_docking_configuration_kwargs_sorted(self, dc_kwargs_list: List[dict]) -> List[dict]:
        """Get unique partial docking configurations (sorted)."""

        logger.debug(f"Getting unique partial docking configurations (sorted). # before: {len(dc_kwargs_list)}")
        new_dc_kwargs = []
        hashes = []
        for dc_kwargs in dc_kwargs_list:
            hash = DockingConfiguration.get_hexdigest_of_persistent_md5_hash_of_docking_configuration_kwargs(dc_kwargs, partial_okay=True)
            if hash not in hashes:
                new_dc_kwargs.append(dc_kwargs)
                hashes.append(hash)

        #
        new_dc_kwargs_sorted, hashes_sorted = zip(*sorted(zip(new_dc_kwargs, hashes), key=lambda x: x[1]))
        logger.debug(f"# after: {len(new_dc_kwargs_sorted)}")

        return new_dc_kwargs_sorted

    def run(
            self, 
            component_run_func_arg_set: DockoptPipelineComponentRunFuncArgSet,
            force_redock: bool,
            force_rewrite_results: bool,
            force_rewrite_report: bool,
        ) -> pd.DataFrame:
        """Run this component of the pipeline."""

        # run necessary steps to get all dock files
        logger.info("Generating docking configurations")
        for dc in self.docking_configurations:
            # make dock files
            for dock_file_identifier in DOCK_FILE_IDENTIFIERS:
                self._run_unrun_steps_needed_to_create_this_blaster_file_node(
                    getattr(dc.dock_file_coordinates, dock_file_identifier).node_id, self.graph
                )

            # make indock file now that dock files exist
            indock_file = dc.get_indock_file(self.pipeline_dir.path)
            indock_file.write(dc.get_dock_files(self.pipeline_dir.path), dc.indock_file_generation_flat_param_dict)

        #
        step_id_file_path = os.path.join(self.component_dir.path, "step_id")
        if File.file_exists(step_id_file_path):
            with open(step_id_file_path, "r") as f:
                step_id, = tuple([line.strip() for line in f.readlines()])
                try:
                    _ = uuid.UUID(step_id)
                except ValueError:
                    raise Exception("step id loaded from step_id_file_path is not a valid UUID.")
        else:
            step_id = str(uuid.uuid4())
            with open(step_id_file_path, "w") as f:
                f.write(f"{step_id}\n")

        # Split self.docking_configurations into chunks of size max_task_array_size
        if component_run_func_arg_set.max_task_array_size is None:
            max_task_array_size = sys.maxsize
        else:
            max_task_array_size = component_run_func_arg_set.max_task_array_size
        docking_configurations_chunks = [self.docking_configurations[i:i + max_task_array_size] for i in
                                         range(0, len(self.docking_configurations), max_task_array_size)]

        chunk_to_array_jobs = {}
        array_job_specs_dir = Dir(os.path.join(self.retrodock_jobs_dir.path, 'array_job_specs'), create=True, reset=False)
        for i, docking_configurations in enumerate(docking_configurations_chunks):
            array_job_docking_configurations_file_path = os.path.join(array_job_specs_dir.path, f"array_job_docking_configurations_{i+1}.txt")
            with open(array_job_docking_configurations_file_path, 'w') as f:
                for dc in docking_configurations:
                    dock_files = dc.get_dock_files(self.pipeline_dir.path)
                    dockfile_paths_str = " ".join([getattr(dock_files, field.name).path for field in fields(dock_files)])
                    indock_file_path_str = dc.get_indock_file(self.pipeline_dir.path).path
                    f.write(f"{dc.configuration_num} {indock_file_path_str} {dockfile_paths_str} {dc.dock_executable_path}\n")

            # submit retrodock jobs (one for actives, one for decoys)
            chunk_array_jobs = []
            for sub_dir_name, should_export_mol2, input_molecules_dir_path in [
                ('actives', True, self.retrospective_dataset.actives_dir_path),
                ('decoys', component_run_func_arg_set.export_decoys_mol2,
                 self.retrospective_dataset.decoys_dir_path),
            ]:
                job_name = f"dockopt_step_{step_id}_{sub_dir_name}_{i+1}"
                sub_dir = Dir(os.path.join(self.retrodock_jobs_dir.path, sub_dir_name), create=True, reset=False)  # task dirs get reset in task submission
                array_job = ArrayDockingJob(
                    name=job_name,
                    job_dir=sub_dir,
                    input_molecules_dir_path=input_molecules_dir_path,
                    job_scheduler=component_run_func_arg_set.scheduler,
                    temp_storage_path=component_run_func_arg_set.temp_storage_path,
                    array_job_docking_configurations_file_path=array_job_docking_configurations_file_path,
                    job_timeout_minutes=component_run_func_arg_set.retrodock_job_timeout_minutes,
                    extra_submission_cmd_params_str=component_run_func_arg_set.extra_submission_cmd_params_str,
                    sleep_seconds_after_copying_output=component_run_func_arg_set.sleep_seconds_after_copying_output,
                    # max_reattempts=component_run_func_arg_set.retrodock_job_max_reattempts,  # TODO
                    export_mol2=should_export_mol2,
                )
                sub_result, procs = array_job.submit_all_tasks(
                    skip_if_complete=(not force_redock),
                )
                chunk_array_jobs.append(array_job)
                log_job_submission_result(array_job, sub_result, procs)

            chunk_to_array_jobs[i] = chunk_array_jobs

        # make a queue of tuples containing job-relevant data for processing
        docking_configurations_processing_queue = collections.deque(deepcopy(self.docking_configurations))

        # process results of docking jobs
        logger.info(
            f"Awaiting / processing ({len(docking_configurations_processing_queue)} tasks in total)"
        )
        data_dicts = []
        task_id_to_num_reattempts_dict = collections.defaultdict(int)
        task_id_to_num_task_output_detection_failed_attempts_dict = collections.defaultdict(int)
        task_id_to_num_task_output_loading_failed_attempts_dict = collections.defaultdict(int)
        max_task_output_detection_reattempts = 1
        max_task_output_loading_reattempts = 1
        datetime_queue_was_last_checked = datetime.min
        task_id_to_datetime_task_output_detection_was_last_attempted_dict = {str(d.configuration_num): datetime.min for d in docking_configurations_processing_queue}
        task_id_to_datetime_task_output_loading_was_last_attempted_dict = {str(d.configuration_num): datetime.min for d in docking_configurations_processing_queue}
        while len(docking_configurations_processing_queue) > 0:
            docking_configuration = docking_configurations_processing_queue.popleft()
            task_id = str(docking_configuration.configuration_num)
            chunk_id = (int(task_id) - 1) // max_task_array_size  # Determine which chunk this task belongs to
            array_jobs = chunk_to_array_jobs[chunk_id]  # Get the corresponding array jobs for this task

            actives_outdock_file_path = os.path.join(self.retrodock_jobs_dir.path, 'actives', task_id, 'OUTDOCK.0')
            decoys_outdock_file_path = os.path.join(self.retrodock_jobs_dir.path, 'decoys', task_id, 'OUTDOCK.0')

            #
            if any([not array_job.task_is_complete(task_id) for array_job in array_jobs]):  # one or both OUTDOCK files do not exist yet
                time.sleep(
                    0.01
                )  # sleep for a bit

                #
                datetime_now = datetime.now()
                if datetime_now < (datetime_queue_was_last_checked + timedelta(seconds=MIN_SECONDS_BETWEEN_QUEUE_CHECKS)):
                    docking_configurations_processing_queue.append(docking_configuration)  # move to back of queue
                    continue  # move on to next in queue in order to more efficiently use time between queue checks

                #
                datetime_queue_was_last_checked = datetime.now()
                if any([job.task_failed(task_id) for job in array_jobs]):
                    #
                    if datetime.now() < (task_id_to_datetime_task_output_detection_was_last_attempted_dict[task_id] + timedelta(seconds=MIN_SECONDS_BETWEEN_TASK_OUTPUT_DETECTION_REATTEMPTS)):
                        task_id_to_datetime_task_output_detection_was_last_attempted_dict[task_id] = datetime.now()
                        docking_configurations_processing_queue.append(docking_configuration)  # move to back of queue
                        continue  # move on to next in queue in order to more efficiently use time between queue checks
                    task_id_to_datetime_task_output_detection_was_last_attempted_dict[task_id] = datetime.now()

                    #
                    task_id_to_num_task_output_detection_failed_attempts_dict[task_id] += 1
                    logger.warning(f"Failed to detect output for task {task_id}")

                    #
                    if task_id_to_num_task_output_detection_failed_attempts_dict[task_id] > max_task_output_detection_reattempts:
                        if task_id_to_num_reattempts_dict[task_id] + 1 > component_run_func_arg_set.retrodock_job_max_reattempts:
                            logger.warning(
                                f"Maximum allowed attempts ({component_run_func_arg_set.retrodock_job_max_reattempts + 1}) exhausted for task {task_id}"
                            )
                            if not component_run_func_arg_set.allow_failed_retrodock_jobs:
                                raise Exception(
                                    f"Failed to complete task {task_id} after {component_run_func_arg_set.retrodock_job_max_reattempts + 1} attempts."
                                )
                            continue  # move on to next in queue without re-attempting failed task
                        else:
                            # re-attempt incomplete task(s)
                            for array_job in array_jobs:
                                if array_job.task_failed(task_id):
                                    array_job.submit_task(
                                        task_id,
                                        skip_if_complete=False,
                                    )
                            task_id_to_num_reattempts_dict[task_id] += 1
                            task_id_to_num_task_output_detection_failed_attempts_dict[task_id] = 0  # reset task failures counter
                            logger.info(
                                f"Re-attempting task {task_id} (attempt {task_id_to_num_reattempts_dict[task_id] + 1} of at most {component_run_func_arg_set.retrodock_job_max_reattempts + 1})"
                            )
                    else:
                        # task must have timed out / failed for one or both jobs
                        logger.warning(
                            f"Failed to detect output for task {task_id}. Will move on in queue and re-attempt once it cycles back around."
                        )
                        time.sleep(1)

                #
                docking_configurations_processing_queue.append(docking_configuration)  # move to back of queue
                continue  # move on to next in queue

            # load outdock files and get dataframe
            try:
                # get dataframe of actives job results and decoys job results combined
                df = get_results_dataframe_from_actives_job_and_decoys_job_outdock_files(
                    actives_outdock_file_path, decoys_outdock_file_path
                )
            except Exception as e:  # if outdock files failed to be parsed then re-attempt task
                try:
                    time.sleep(0.01)  # sleep for a bit and try again
                    df = get_results_dataframe_from_actives_job_and_decoys_job_outdock_files(
                        actives_outdock_file_path, decoys_outdock_file_path
                    )
                except Exception as e:
                    #
                    if datetime.now() < (task_id_to_datetime_task_output_loading_was_last_attempted_dict[task_id] + timedelta(seconds=MIN_SECONDS_BETWEEN_TASK_OUTPUT_LOADING_REATTEMPTS)):
                        task_id_to_datetime_task_output_loading_was_last_attempted_dict[task_id] = datetime.now()
                        docking_configurations_processing_queue.append(docking_configuration)  # move to back of queue
                        continue  # move on to next in queue in order to more efficiently use time between queue checks
                    task_id_to_datetime_task_output_loading_was_last_attempted_dict[task_id] = datetime.now()

                    #
                    task_id_to_num_task_output_loading_failed_attempts_dict[task_id] += 1
                    logger.warning(f"Failed to load output for task {task_id} due to error: {e}")

                    #
                    if task_id_to_num_task_output_loading_failed_attempts_dict[task_id] > max_task_output_detection_reattempts:
                        if task_id_to_num_reattempts_dict[task_id] + 1 > component_run_func_arg_set.retrodock_job_max_reattempts:
                            logger.warning(
                                f"Maximum allowed attempts ({component_run_func_arg_set.retrodock_job_max_reattempts + 1}) exhausted for task {task_id}"
                            )
                            if not component_run_func_arg_set.allow_failed_retrodock_jobs:
                                raise Exception(
                                    f"Failed to complete task {task_id} after {component_run_func_arg_set.retrodock_job_max_reattempts + 1} attempts."
                                )
                            continue  # move on to next in queue without re-attempting failed task
                        else:
                            for array_job, outdock_file_path in zip(array_jobs, [actives_outdock_file_path, decoys_outdock_file_path]):
                                try:
                                    _ = OutdockFile(outdock_file_path).get_dataframe()  # only resubmit if outdock file can't be loaded
                                except Exception as e:
                                    array_job.submit_task(
                                        task_id,
                                        skip_if_complete=False,
                                    )
                            task_id_to_num_reattempts_dict[task_id] += 1
                            logger.info(
                                f"Re-attempting task {task_id} (attempt {task_id_to_num_reattempts_dict[task_id] + 1} of at most {component_run_func_arg_set.retrodock_job_max_reattempts + 1})"
                            )
                    else:
                        logger.warning(
                            f"Failed to load output for task {task_id}. Will move on in queue and re-attempt once it cycles back around."
                        )
                        time.sleep(1)

                    #
                    docking_configurations_processing_queue.append(docking_configuration)  # move to back of queue
                    continue  # move on to next in queue

            #
            logger.info(
                f"Task {task_id} complete. Loaded both OUTDOCK files."
            )

            # validate scored molecules
            num_active_db2_files_scored = df[df['is_active'].astype(bool)]['db2_file_path'].nunique()
            num_decoy_db2_files_scored = df[~df['is_active'].astype(bool)]['db2_file_path'].nunique()

            if num_active_db2_files_scored != self.retrospective_dataset.num_db2_files_in_active_class:
                raise Exception(
                    f"Retrospective dataset has {self.retrospective_dataset.num_db2_files_in_active_class} DB2 files in active class but only detected {num_active_db2_files_scored} while processing retrodock job for task {task_id}")
            if num_decoy_db2_files_scored != self.retrospective_dataset.num_db2_files_in_decoy_class:
                raise Exception(
                    f"Retrospective dataset has {self.retrospective_dataset.num_db2_files_in_decoy_class} DB2 files in decoy class but only detected {num_decoy_db2_files_scored} while processing retrodock job for task {task_id}")

            # sort dataframe by total energy score and drop duplicate molecules
            df = sort_by_energy_and_drop_duplicate_molecules(df)

            # make data dict for this configuration num
            data_dict = docking_configuration.to_dict()

            # get ROC and calculate normalized LogAUC of this job's docking set-up
            if isinstance(self.criterion, NormalizedLogAUC):  # TODO: generalize `self.criterion` such that this ad hoc check is not necessary
                booleans = df["is_active"]
                data_dict[self.criterion.name] = self.criterion.calculate(booleans)

            # save data_dict for this job
            data_dicts.append(data_dict)

        # write jobs completion status
        num_tasks_successful = len(data_dicts)
        logger.info(
            f"Finished {num_tasks_successful} out of {len(self.docking_configurations)} tasks."
        )

        #
        if num_tasks_successful != len(self.docking_configurations):
            if not component_run_func_arg_set.allow_failed_retrodock_jobs:
                raise Exception(
                    f"Failed {len(self.docking_configurations) - num_tasks_successful} out of {len(self.docking_configurations)} tasks. Failed tasks are not allowed. Exiting."
                )

        #
        if num_tasks_successful == 0:
            raise Exception(
                "All tasks failed. Something is wrong."
            )

        # make dataframe of optimization job results
        logger.info("Making dataframe of results")
        df = pd.DataFrame(data=data_dicts)

        #
        if component_run_func_arg_set.delete_intermediate_files:
            logger.info("Deleting intermediate files...")

            # Sorting dataframe to ensure we have the top_n rows correctly
            df.sort_values(by=self.criterion.name, ascending=False, inplace=True)

            # Get the list of directories we want to keep
            keep_dirs = df.head(self.top_n)['configuration_num'].apply(str).tolist()

            # Deleting directories not in top_n
            for class_identifier in ['actives', 'decoys']:
                class_dir = os.path.join(self.retrodock_jobs_dir.path, class_identifier)
                for obj in os.listdir(class_dir):
                    obj_path = os.path.join(class_dir, obj)
                    if obj not in keep_dirs:
                        if os.path.isdir(obj_path):
                            shutil.rmtree(obj_path)
                        elif os.path.isfile(obj_path):
                            os.remove(obj_path)

            # Deleting files from working not present in the top_n rows
            # We'll need a list of files to keep
            keep_files = []
            for _, row in df.head(self.top_n).iterrows():
                for column in row.index:
                    if column.startswith("dock_files.") or column.startswith("indock_file."):
                        keep_files.append(row[column])

            # Deleting files not in keep_files
            for obj in os.listdir(self.working_dir.path):
                obj_path = os.path.join(self.working_dir.path, obj)
                if obj not in keep_files:
                    if os.path.isfile(obj_path):
                        os.remove(obj_path)
                    elif os.path.isdir(obj_path):
                        shutil.rmtree(obj_path)

            logger.info("done.")

        return df

    @staticmethod
    def _get_dock_file_lineage_subgraph(graph: nx.DiGraph, dock_file_node_id: str) -> nx.DiGraph:
        """Gets the subgraph representing the steps necessary to produce the desired dock file"""

        node_ids = [dock_file_node_id] + list(nx.ancestors(graph, dock_file_node_id))
        step_hashes = []
        for u, v, data in graph.edges(data=True):
            if u in node_ids and v in node_ids:
                step_hashes.append(data['step_hash'])
        for u, v, data in graph.edges(data=True):
            if data['step_hash'] in step_hashes:
                node_ids.append(u)
                node_ids.append(v)
        node_ids = list(set(node_ids))

        return graph.subgraph(node_ids)

    @staticmethod
    def _get_infile_hash(component_id: str, infile: BlasterFile) -> str:
        """Returns a hash of the infile's class name and original_file_in_working_dir name"""

        return get_hexdigest_of_persistent_md5_hash_of_tuple((component_id, infile.original_file_in_working_dir.name))

    @staticmethod
    def _get_outfile_hash(component_id: str, outfile: BlasterFile, step_hash: str) -> str:
        """Returns a hash of the outfile's class name, original_file_in_working_dir name, and step_hash"""

        return get_hexdigest_of_persistent_md5_hash_of_tuple((component_id, outfile.original_file_in_working_dir.name, step_hash))

    @staticmethod
    def _get_step_hash(component_id: str, step: BlasterStep, infiles_hash: str) -> str:
        """Returns a hash of the step's class name, step_dir name, infiles, parameters, and outfiles"""

        #
        parameters_dict_items_list = sorted(step.parameters._asdict().items())
        outfiles_dict_items_list = sorted(step.outfiles._asdict().items())

        #
        return get_hexdigest_of_persistent_md5_hash_of_tuple(
            tuple(
                [step.__class__.__name__, step.step_dir.name]
                + [infiles_hash]
                + [(parameter_step_var_name, parameter.hexdigest_of_persistent_md5_hash) for parameter_step_var_name, parameter in parameters_dict_items_list]
                + [(outfile_step_var_name, outfile.original_file_in_working_dir.name) for outfile_step_var_name, outfile in outfiles_dict_items_list]
            )
        )

    @staticmethod
    def _get_graph_from_all_steps_in_order(component_id: str, steps: List[BlasterStep]) -> nx.DiGraph:
        """Returns a graph of all steps in order of execution"""

        #
        graph = nx.DiGraph()
        blaster_file_hash_dict = {}
        for step in steps:
            #
            infiles_dict_items_list = sorted(step.infiles._asdict().items())
            outfiles_dict_items_list = sorted(step.outfiles._asdict().items())
            parameters_dict_items_list = sorted(step.parameters._asdict().items())

            # add infile nodes
            infile_hashes = []
            for infile in step.infiles:
                if DockoptStep._get_blaster_file_node_with_same_file_name(infile.original_file_in_working_dir.name, graph) is not None:
                    infile_hashes.append(blaster_file_hash_dict[(component_id, infile.original_file_in_working_dir.name)])
                    continue
                if (component_id, infile.original_file_in_working_dir.name) not in blaster_file_hash_dict:
                    blaster_file_hash_dict[(component_id, infile.original_file_in_working_dir.name)] = DockoptStep._get_infile_hash(component_id, infile)
                graph.add_node(
                    blaster_file_hash_dict[(component_id, infile.original_file_in_working_dir.name)],
                    blaster_file=deepcopy(infile.original_file_in_working_dir),
                )
                infile_hashes.append(blaster_file_hash_dict[(component_id, infile.original_file_in_working_dir.name)])
            infiles_hash = get_hexdigest_of_persistent_md5_hash_of_tuple(tuple(sorted(infile_hashes)))

            # get step hash from infile hashes, step dir, parameters, and outfiles
            step_hash = DockoptStep._get_step_hash(component_id, step, infiles_hash)

            # add outfile nodes
            for outfile_step_var_name, outfile in outfiles_dict_items_list:
                if DockoptStep._get_blaster_file_node_with_same_file_name(
                        outfile.original_file_in_working_dir.name, graph
                ):
                    raise Exception(
                        f"Attempting to add outfile to graph that already has said outfile as node: {outfile.original_file_in_working_dir.name}"
                    )
                if (component_id, outfile.original_file_in_working_dir.name) not in blaster_file_hash_dict:
                    blaster_file_hash_dict[(component_id, outfile.original_file_in_working_dir.name)] = DockoptStep._get_outfile_hash(component_id, outfile, step_hash)
                graph.add_node(
                    blaster_file_hash_dict[(component_id, outfile.original_file_in_working_dir.name)],
                    blaster_file=deepcopy(outfile.original_file_in_working_dir),
                )

            # add parameter nodes
            for parameter in step.parameters:
                graph.add_node(parameter.hexdigest_of_persistent_md5_hash, parameter=deepcopy(parameter))

            # connect each infile node to every outfile node
            for (infile_step_var_name, infile), (outfile_step_var_name, outfile) in itertools.product(
                infiles_dict_items_list, outfiles_dict_items_list
            ):
                graph.add_edge(
                    blaster_file_hash_dict[(component_id, infile.original_file_in_working_dir.name)],
                    blaster_file_hash_dict[(component_id, outfile.original_file_in_working_dir.name)],
                    step_class=step.__class__,
                    original_step_dir_name=step.step_dir.name,
                    step_instance=deepcopy(step),
                    step_hash=step_hash,
                    parent_node_step_var_name=infile_step_var_name,
                    child_node_step_var_name=outfile_step_var_name,
                )

            # connect each parameter node to every outfile nodes
            for (parameter_step_var_name, parameter), (outfile_step_var_name, outfile) in itertools.product(
                parameters_dict_items_list, outfiles_dict_items_list
            ):
                graph.add_edge(
                    parameter.hexdigest_of_persistent_md5_hash,
                    blaster_file_hash_dict[(component_id, outfile.original_file_in_working_dir.name)],
                    step_class=step.__class__,
                    original_step_dir_name=step.step_dir.name,
                    step_instance=deepcopy(step),  # this will be replaced with step instance with unique dir path
                    step_hash=step_hash,
                    parent_node_step_var_name=parameter_step_var_name,
                    child_node_step_var_name=outfile_step_var_name,
                )

        return graph

    @staticmethod
    def _get_blaster_file_nodes(g: nx.DiGraph) -> str:
        """Get blaster file nodes."""

        return [node_id for node_id, node_data in g.nodes.items() if g.nodes[node_id].get("blaster_file")]

    @staticmethod
    def _get_blaster_file_node_with_blaster_file_identifier(
        blaster_file_identifier: str,
        g: nx.DiGraph,
    ) -> str:
        """Get blaster file node with blaster file identifier blaster_file_identifier."""

        blaster_file_node_ids = DockoptStep._get_blaster_file_nodes(g)
        if len(blaster_file_node_ids) == 0:
            return None
        matching_blaster_file_nodes = [node_id for node_id in blaster_file_node_ids if g.nodes[node_id]["blaster_file"].identifier == blaster_file_identifier]
        if len(matching_blaster_file_nodes) == 0:
            return None
        matching_blaster_file_node, = matching_blaster_file_nodes

        return matching_blaster_file_node

    @staticmethod
    def _get_blaster_file_node_with_same_file_name(
        file_name: str,
        g: nx.DiGraph,
    ) -> BlasterFile:
        """Get blaster file node with same file name as file_name."""

        blaster_file_node_ids = DockoptStep._get_blaster_file_nodes(g)
        if len(blaster_file_node_ids) == 0:
            return None
        matching_blaster_file_nodes = [node_id for node_id in blaster_file_node_ids if file_name == g.nodes[node_id]["blaster_file"].name]
        if len(matching_blaster_file_nodes) == 0:
            return None
        matching_blaster_file_node, = matching_blaster_file_nodes

        return matching_blaster_file_node

    @staticmethod
    def _get_dock_files_generation_flat_param_dict(graph: nx.DiGraph, dock_file_coordinates: DockFileCoordinates) -> dict:
        """Get a flat dict of parameters needed to generate dock files from dock file nodes in graph."""

        dock_file_node_ids = sorted([getattr(dock_file_coordinates, field.name).node_id for field in fields(dock_file_coordinates)])
        node_ids = [node_id for dock_file_node_id in dock_file_node_ids for node_id in nx.ancestors(graph, dock_file_node_id)]
        node_ids = list(set(node_ids))
        d = {}
        for node_id in node_ids:
            if graph.nodes[node_id].get('parameter'):
                parameter = graph.nodes[node_id]['parameter']
                d[parameter.name] = parameter.value
        logger.debug(f"dock files generation parameters dict derived from dock file nodes:\n\tnodes: {dock_file_node_ids}\n\tdict: {d}")

        return d

    @staticmethod
    def _run_unrun_steps_needed_to_create_this_blaster_file_node(
        blaster_file_node: str,
        g: nx.DiGraph,
    ) -> None:
        """Run all steps needed to create this blaster file node."""

        if g.nodes[blaster_file_node].get("blaster_file") is not None:
            blaster_file = g.nodes[blaster_file_node]['blaster_file']
            if not blaster_file.exists:
                for parent_node in g.predecessors(blaster_file_node):
                    DockoptStep._run_unrun_steps_needed_to_create_this_blaster_file_node(
                        parent_node, g
                    )
                a_parent_node = list(g.predecessors(blaster_file_node))[0]
                step_instance = g[a_parent_node][blaster_file_node]["step_instance"]
                if step_instance.is_done:
                    raise Exception(
                        f"blaster file {blaster_file.path} does not exist but step instance is_done=True"
                    )
                step_instance.run()


class DockoptStepSequenceIteration(PipelineComponentSequenceIteration):

    def __init__(
        self,
        pipeline_dir_path: str,
        component_id: str,
        criterion: str,
        top_n: int,
        components: Iterable[dict],
        retrospective_dataset: RetrospectiveDataset,
        blaster_files_to_copy_in: Iterable[BlasterFile],
        last_component_completed: Union[PipelineComponent, None] = None,
    ) -> None:
        super().__init__(
            pipeline_dir_path=pipeline_dir_path,
            component_id=component_id,
            criterion=criterion,
            top_n=top_n,
            results_manager=DockoptStepSequenceIterationResultsManager(RESULTS_CSV_FILE_NAME),
            components=components,
        )

        #
        self.retrospective_dataset = retrospective_dataset

        #
        self.blaster_files_to_copy_in = blaster_files_to_copy_in
        self.last_component_completed = last_component_completed

        #
        self.best_retrodock_jobs_dir = Dir(
            path=os.path.join(self.component_dir.path, BEST_RETRODOCK_JOBS_DIR_NAME),
            create=True,
            reset=False,
        )

        #
        self.graph = nx.DiGraph()

    def run(
            self, 
            component_run_func_arg_set: DockoptPipelineComponentRunFuncArgSet,
            force_redock: bool,
            force_rewrite_results: bool,
            force_rewrite_report: bool,
            ) -> pd.DataFrame:
        """Run the pipeline component sequence iteration."""

        df = pd.DataFrame()
        best_criterion_value_witnessed = -float('inf')
        last_component_completed_in_sequence = self.last_component_completed
        for i, component_identifier_dict in enumerate(self.components):
            #
            component_num = i + 1

            #
            if "step" in component_identifier_dict:
                component_identifier = "step"
                component_class = DockoptStep
                component_id = f"{self.component_id}.{component_num}_step"
            elif "sequence" in component_identifier_dict:
                component_identifier = "sequence"
                component_class = DockoptStepSequence
                component_id = f"{self.component_id}.{component_num}_seq"
            else:
                raise Exception(f"Dict must have one of 'step' or 'sequence' as keys. Witnessed: {component_identifier_dict}")

            #
            component_parameters_dict = deepcopy(component_identifier_dict[component_identifier])
            parameters_manager = DockoptComponentParametersManager(
                parameters_dict=component_parameters_dict,
                last_component_completed=last_component_completed_in_sequence,
            )

            #
            kwargs = filter_kwargs_for_callable({
                **parameters_manager.parameters_dict,
                'component_id': component_id,
                'pipeline_dir_path': self.pipeline_dir.path,
                'retrospective_dataset': self.retrospective_dataset,
                'blaster_files_to_copy_in': self.blaster_files_to_copy_in,  # TODO: is this necessary?
                'last_component_completed': last_component_completed_in_sequence,
            }, component_class)
            component = component_class(**kwargs)
            component.run(
                component_run_func_arg_set,
                force_redock=force_redock,
                force_rewrite_results=force_rewrite_results,
                force_rewrite_report=force_rewrite_report,
            )

            #
            df_component = component.load_results_dataframe()
            df = pd.concat([df, df_component], ignore_index=True)

            #
            last_component_completed_in_sequence = component

            # TODO: make sure this is memory efficient
            self.graph = nx.compose(self.graph, last_component_completed_in_sequence.graph)

        #
        self.last_component_completed = last_component_completed_in_sequence

        return df

    @property
    def num_total_docking_configurations_thus_far(self):
        """Returns the total number of docking configurations that have been run thus far in this iteration of the pipeline."""

        return self.last_component_completed.num_total_docking_configurations_thus_far


class DockoptStepSequence(PipelineComponentSequence):
    def __init__(
        self,
        pipeline_dir_path: str,
        component_id: str,
        criterion: str,
        top_n: int,
        components: Iterable[dict],
        retrospective_dataset: RetrospectiveDataset,
        num_iterations: int,
        max_iterations_with_no_improvement: int,
        inter_iteration_criterion: str,
        inter_iteration_top_n: int,
        blaster_files_to_copy_in: Iterable[BlasterFile],
        last_component_completed: Union[PipelineComponent, None] = None,
    ) -> None:
        super().__init__(
            pipeline_dir_path=pipeline_dir_path,
            component_id=component_id,
            criterion=criterion,
            top_n=top_n,
            results_manager=DockoptStepSequenceResultsManager(RESULTS_CSV_FILE_NAME),
            components=components,
            num_iterations=num_iterations,
            max_iterations_with_no_improvement=max_iterations_with_no_improvement,
            inter_iteration_criterion=inter_iteration_criterion,
            inter_iteration_top_n=inter_iteration_top_n,
        )

        #
        self.retrospective_dataset = retrospective_dataset

        #
        self.blaster_files_to_copy_in = blaster_files_to_copy_in
        self.last_component_completed = last_component_completed

        #
        self.best_retrodock_jobs_dir = Dir(
            path=os.path.join(self.component_dir.path, BEST_RETRODOCK_JOBS_DIR_NAME),
            create=True,
            reset=False,
        )

        #
        self.graph = nx.DiGraph()

    def run(
            self, 
            component_run_func_arg_set: DockoptPipelineComponentRunFuncArgSet,
            force_redock: bool,
            force_rewrite_results: bool,
            force_rewrite_report: bool,
            ) -> pd.DataFrame:
        df = pd.DataFrame()
        best_criterion_value_witnessed = -float('inf')
        last_component_completed_in_sequence = self.last_component_completed
        num_iterations_left_with_no_improvement = self.max_iterations_with_no_improvement
        for i in range(self.num_iterations):
            #
            iteration_num =  i + 1

            #
            component = DockoptStepSequenceIteration(
                component_id=f"{self.component_id}.{iteration_num}_iter",
                pipeline_dir_path=self.pipeline_dir.path,
                criterion=self.inter_iteration_criterion,
                top_n=self.inter_iteration_top_n,
                components=self.components,
                retrospective_dataset=self.retrospective_dataset,
                blaster_files_to_copy_in=self.blaster_files_to_copy_in,
                last_component_completed=last_component_completed_in_sequence,
            )
            component.run(
                component_run_func_arg_set,
                force_redock=force_redock,
                force_rewrite_results=force_rewrite_results,
                force_rewrite_report=force_rewrite_report,
            )

            #
            df_component = component.load_results_dataframe()
            df = pd.concat([df, df_component], ignore_index=True)

            #
            last_component_completed_in_sequence = component

            # TODO: make sure this is memory efficient
            self.graph = nx.compose(self.graph, last_component_completed_in_sequence.graph)

            #
            best_criterion_value_witnessed_this_iteration = df_component[component.criterion.name].max()
            if best_criterion_value_witnessed_this_iteration <= best_criterion_value_witnessed:
                if num_iterations_left_with_no_improvement == 0:
                    break
                else:
                    num_iterations_left_with_no_improvement -= 1
            else:
                best_criterion_value_witnessed = best_criterion_value_witnessed_this_iteration

        #
        self.last_component_completed = last_component_completed_in_sequence

        return df

    @property
    def num_total_docking_configurations_thus_far(self):
        """Returns the total number of docking configurations that have been generated thus far in the pipeline."""

        return self.last_component_completed.num_total_docking_configurations_thus_far


class DockoptPipeline(Pipeline):
    def __init__(
            self,
            pipeline_dir_path: str,
            criterion: str,
            top_n: int,
            components: Iterable[dict],
            retrospective_dataset: RetrospectiveDataset,
            blaster_files_to_copy_in: Iterable[BlasterFile],
            last_component_completed: Union[PipelineComponent, None] = None,
    ) -> None:
        super().__init__(
            pipeline_dir_path=pipeline_dir_path,
            criterion=criterion,
            top_n=top_n,
            results_manager=DockoptStepSequenceIterationResultsManager(RESULTS_CSV_FILE_NAME),
            components=components,
        )

        #
        self.retrospective_dataset = retrospective_dataset

        #
        self.blaster_files_to_copy_in = blaster_files_to_copy_in
        self.last_component_completed = last_component_completed

        #
        self.best_retrodock_jobs_dir = Dir(
            path=os.path.join(self.pipeline_dir.path, BEST_RETRODOCK_JOBS_DIR_NAME),
            create=True,
            reset=False,
        )

        #
        self.graph = nx.DiGraph()

    def run(
            self, 
            component_run_func_arg_set: DockoptPipelineComponentRunFuncArgSet,
            force_redock: bool,
            force_rewrite_results: bool,
            force_rewrite_report: bool,
            ) -> pd.DataFrame:
        """Run the pipeline."""

        df = pd.DataFrame()
        best_criterion_value_witnessed = -float('inf')
        last_component_completed_in_sequence = self.last_component_completed
        for i, component_identifier_dict in enumerate(self.components):
            #
            component_num = i + 1

            #
            if "step" in component_identifier_dict:
                component_identifier = "step"
                component_class = DockoptStep
                component_id = f"{component_num}_step"
            elif "sequence" in component_identifier_dict:
                component_identifier = "sequence"
                component_class = DockoptStepSequence
                component_id = f"{component_num}_seq"
            else:
                raise Exception(
                    f"Dict must have one of 'step' or 'sequence' as keys. Witnessed: {component_identifier_dict}")

            #
            component_parameters_dict = deepcopy(component_identifier_dict[component_identifier])
            parameters_manager = DockoptComponentParametersManager(
                parameters_dict=component_parameters_dict,
                last_component_completed=last_component_completed_in_sequence,
            )

            #
            kwargs = filter_kwargs_for_callable({
                **parameters_manager.parameters_dict,
                'component_id': component_id,
                'pipeline_dir_path': self.pipeline_dir.path,
                'retrospective_dataset': self.retrospective_dataset,
                'blaster_files_to_copy_in': self.blaster_files_to_copy_in,  # TODO: is this necessary?
                'last_component_completed': last_component_completed_in_sequence,
            }, component_class)
            component = component_class(**kwargs)
            component.run(
                component_run_func_arg_set,
                force_redock=force_redock,
                force_rewrite_results=force_rewrite_results,
                force_rewrite_report=force_rewrite_report,
            )

            #
            df_component = component.load_results_dataframe()
            df = pd.concat([df, df_component], ignore_index=True)

            #
            last_component_completed_in_sequence = component

            # TODO: make sure this is memory efficient
            self.graph = nx.compose(self.graph, last_component_completed_in_sequence.graph)

        #
        self.last_component_completed = last_component_completed_in_sequence

        return df

    @property
    def num_total_docking_configurations_thus_far(self):
        """Returns the total number of docking configurations that have been generated thus far in the pipeline."""

        return self.last_component_completed.num_total_docking_configurations_thus_far
