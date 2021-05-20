# -*- Mode:Python; indent-tabs-mode:nil; tab-width:4 -*-
#
# Copyright 2021 Canonical Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""The parts lifecycle manager."""

import re
from pathlib import Path
from typing import Any, Dict, List, Sequence, Union

from pydantic import ValidationError

from craft_parts import errors, executor, packages, plugins, sequencer
from craft_parts.actions import Action
from craft_parts.dirs import ProjectDirs
from craft_parts.infos import ProjectInfo
from craft_parts.parts import Part
from craft_parts.steps import Step


class LifecycleManager:
    """Coordinate the planning and execution of the parts lifecycle.

    The lifecycle manager determines the list of actions that needs be executed in
    order to obtain a tree of installed files from the specification on how to
    process its parts, and provides a mechanism to execute each of these actions.

    :param all_parts: A dictionary containing the parts specification according
        to the :ref:`parts schema<parts-schema>`. The format is compatible with the
        output generated by PyYAML's ``yaml.load``.
    :param application_name: A unique non-empty identifier for the application
        using Craft Parts. The application name is used as a prefix to environment
        variables set during step execution. Valid application names contain upper
        and lower case letters, underscores or numbers, and must start with a letter.
    :param cache_dir: The path to store cached packages and files. If not
        specified, a directory under the application name entry in the XDG base
        directory will be used.
    :param work_dir: The toplevel directory for work directories. The current
        directory will be used if none is specified.
    :param arch: The architecture to build for. Defaults to the host system
        architecture.
    :param base: The system base the project being processed will run on. Defaults
        to the system where Craft Parts is being executed.
    :param parallel_build_count: The maximum number of concurrent jobs to be
        used to build each part of this project.
    :param custom_args: Any additional arguments that will be passed directly
        to :ref:`callbacks<callbacks>`.
    """

    def __init__(
        self,
        all_parts: Dict[str, Any],
        *,
        application_name: str,
        cache_dir: Union[Path, str],
        work_dir: str = ".",
        arch: str = "",
        base: str = "",
        parallel_build_count: int = 1,
        extra_build_packages: List[str] = None,
        **custom_args,  # custom passthrough args
    ):
        if not re.match("^[A-Za-z][0-9A-Za-z_]*$", application_name):
            raise errors.InvalidApplicationName(application_name)

        if not isinstance(all_parts, dict):
            raise TypeError("parts definition must be a dictionary")

        if "parts" not in all_parts:
            raise ValueError("parts definition is missing")

        project_dirs = ProjectDirs(work_dir=work_dir)

        project_info = ProjectInfo(
            application_name=application_name,
            cache_dir=Path(cache_dir),
            arch=arch,
            base=base,
            parallel_build_count=parallel_build_count,
            project_dirs=project_dirs,
            **custom_args,
        )

        parts_data = all_parts.get("parts", {})

        part_list = []
        for name, spec in parts_data.items():
            part_list.append(_build_part(name, spec, project_dirs))

        self._part_list = part_list
        self._application_name = application_name
        self._target_arch = project_info.target_arch
        self._sequencer = sequencer.Sequencer(
            part_list=self._part_list,
            project_info=project_info,
        )
        self._executor = executor.Executor(
            part_list=self._part_list,
            project_info=project_info,
            extra_build_packages=extra_build_packages,
        )
        self._project_info = project_info

    @property
    def project_info(self) -> ProjectInfo:
        """Obtain information about this project."""
        return self._project_info

    def clean(self, step: Step = Step.PULL, *, part_names: List[str] = None) -> None:
        """Clean the specified step and parts.

        Cleaning a step removes its state and all artifacts generated in that
        step and subsequent steps for the specified parts.

        :para step: The step to clean. If not specified, all steps will be
            cleaned.
        :param part_names: The list of part names to clean. If not specified,
            all parts will be cleaned and work directories will be removed.
        """
        self._executor.clean(initial_step=step, part_names=part_names)

    def refresh_packages_list(self, *, system=False) -> None:
        """Update the available packages list.

        The list of available packages should be updated before planning the
        sequence of actions to take. To ensure consistency between the scenarios,
        it shouldn't be updated between planning and execution.

        :param system: Also refresh the list of available build packages to
            install on the host system.
        """
        packages.Repository.refresh_stage_packages_list(
            cache_dir=self._project_info.cache_dir, target_arch=self._target_arch
        )

        if system:
            packages.Repository.refresh_build_packages_list()

    def plan(self, target_step: Step, part_names: Sequence[str] = None) -> List[Action]:
        """Obtain the list of actions to be executed given the target step and parts.

        :param target_step: The final step we want to reach.
        :param part_names: The list of parts to process. If not specified, all
            parts will be processed.
        :param update: Refresh the list of available packages.

        :return: The list of :class:`Action` objects that should be executed in
            order to reach the target step for the specified parts.
        """
        actions = self._sequencer.plan(target_step, part_names)
        return actions

    def reload_state(self) -> None:
        """Reload the ephemeral state from disk."""
        self._sequencer.reload_state()

    def action_executor(self) -> executor.ExecutionContext:
        """Return a context manager for action execution."""
        return executor.ExecutionContext(executor=self._executor)


def _build_part(name: str, spec: Dict[str, Any], project_dirs: ProjectDirs) -> Part:
    """Create and populate a :class:`Part` object based on part specification data.

    :param spec: A dictionary containing the part specification.
    :param project_dirs: The project's work directories.

    :return: A :class:`Part` object corresponding to the given part specification.
    """
    if not isinstance(spec, dict):
        raise errors.PartSpecificationError(
            part_name=name, message="part definition is malformed"
        )

    plugin_name = spec.get("plugin", "")

    # If the plugin was not specified, use the part name as the plugin name.
    part_name_as_plugin_name = not plugin_name
    if part_name_as_plugin_name:
        plugin_name = name

    try:
        plugin_class = plugins.get_plugin_class(plugin_name)
    except ValueError as err:
        if part_name_as_plugin_name:
            # If plugin was not specified, avoid raising an exception telling
            # that part name is an invalid plugin.
            raise errors.UndefinedPlugin(part_name=name) from err
        raise errors.InvalidPlugin(plugin_name, part_name=name) from err

    # validate and unmarshal plugin properties
    try:
        properties = plugin_class.properties_class.unmarshal(spec)
    except ValidationError as err:
        raise errors.PartSpecificationError.from_validation_error(
            part_name=name, error_list=err.errors()
        ) from err
    except ValueError as err:
        raise errors.PartSpecificationError(part_name=name, message=str(err)) from err

    plugins.strip_plugin_properties(spec, plugin_name=plugin_name)

    # initialize part and unmarshal part specs
    part = Part(name, spec, project_dirs=project_dirs, plugin_properties=properties)

    return part
