import os
import logging

from dataclasses import dataclass

from pydock3.config import ParametersConfiguration
from pydock3.dockopt import __file__ as DOCKOPT_INIT_FILE_PATH


#
DOCKOPT_CONFIG_SCHEMA_FILE_PATH = os.path.join(
    os.path.dirname(DOCKOPT_INIT_FILE_PATH), "dockopt_config_schema.yaml"
)

# Simple config addition
DOCKOPT_SIMPLE_CONFIG_SCHEMA_FILE_PATH = os.path.join(
    os.path.dirname(DOCKOPT_INIT_FILE_PATH), "dockopt_simple_config_schema.yaml"
)

#
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


class DockoptParametersConfiguration(ParametersConfiguration):
    def __init__(self, config_file_path, simple=False):
        schema_file_path = DOCKOPT_SIMPLE_CONFIG_SCHEMA_FILE_PATH if simple else DOCKOPT_CONFIG_SCHEMA_FILE_PATH
        super().__init__(
            config_file_path=config_file_path,
            schema_file_path=schema_file_path,
        )
    

    def override_w_simple_config(self, simple_config):
        """
        Zack Mawaldi
        Overrides the current configuration with values from the simple config... Manually.
        I know it's cursed, but given that steps is a list, I found this to be the easiest to understand solution.
        
        Args:
            simple_config (DockoptParametersConfiguration): The simple configuration object to override with.
        """
        # Extract the parameter dictionaries
        advanced = self.param_dict
        simple = simple_config.param_dict

        # Perform the overrides
        advanced['definitions']['steps'][0]['step']['top_n'] = simple['definitions']['steps'][0]['step']['top_n']
        advanced['definitions']['steps'][1]['step']['top_n'] = simple['definitions']['steps'][0]['step']['top_n']
        
        advanced['definitions']['steps'][0]['step']['parameters']['dock_files_generation']['thin_spheres_elec']['use'] = \
          simple['definitions']['steps'][0]['step']['parameters']['dock_files_generation']['thin_spheres_elec']['use']
        
        advanced['definitions']['steps'][0]['step']['parameters']['dock_files_generation']['thin_spheres_elec']['distance_to_surface'] = \
          simple['definitions']['steps'][0]['step']['parameters']['dock_files_generation']['thin_spheres_elec']['distance_to_surface']

        advanced['definitions']['steps'][0]['step']['parameters']['dock_files_generation']['thin_spheres_desolv']['distance_to_surface'] = \
          simple['definitions']['steps'][0]['step']['parameters']['dock_files_generation']['thin_spheres_desolv']['distance_to_surface']
        
        advanced['definitions']['steps'][0]['step']['parameters']['dock_files_generation']['thin_spheres_desolv']['use'] = \
          simple['definitions']['steps'][0]['step']['parameters']['dock_files_generation']['thin_spheres_desolv']['use']

        advanced['definitions']['steps'][0]['step']['parameters']['dock_files_modification']['matching_spheres_perturbation'] = \
          simple['definitions']['steps'][0]['step']['parameters']['dock_files_modification']['matching_spheres_perturbation']

        advanced['pipeline']['top_n'] = simple['pipeline']['top_n']
