import os

from typing import TYPE_CHECKING
from typing import Dict
from typing import List
from typing import cast

from cleo.helpers import argument
from cleo.helpers import option

from ..init import InitCommand


if TYPE_CHECKING:
    from pathlib import Path

    from poetry.console.application import Application  # noqa
    from poetry.console.commands.update import UpdateCommand
    from poetry.packages.project_package import ProjectPackage


class PluginAddCommand(InitCommand):

    name = "plugin add"

    description = "Adds new plugins."

    arguments = [
        argument("plugins", "The names of the plugins to install.", multiple=True),
    ]

    options = [
        option(
            "dry-run",
            None,
            "Output the operations but do not execute anything (implicitly enables --verbose).",
        )
    ]

    help = """
The <c1>plugin add</c1> command installs Poetry plugins globally.

It works similarly to the <c1>add</c1> command:

If you do not specify a version constraint, poetry will choose a suitable one based on the available package versions.

You can specify a package in the following forms:

  - A single name (<b>requests</b>)
  - A name and a constraint (<b>requests@^2.23.0</b>)
  - A git url (<b>git+https://github.com/python-poetry/poetry.git</b>)
  - A git url with a revision (<b>git+https://github.com/python-poetry/poetry.git#develop</b>)
  - A git SSH url (<b>git+ssh://github.com/python-poetry/poetry.git</b>)
  - A git SSH url with a revision (<b>git+ssh://github.com/python-poetry/poetry.git#develop</b>)
  - A file path (<b>../my-package/my-package.whl</b>)
  - A directory (<b>../my-package/</b>)
  - A url (<b>https://example.com/packages/my-package-0.1.0.tar.gz</b>)\
"""

    def handle(self) -> int:
        from pathlib import Path

        import tomlkit

        from cleo.io.inputs.string_input import StringInput
        from cleo.io.io import IO

        from poetry.core.semver import parse_constraint
        from poetry.factory import Factory
        from poetry.packages.project_package import ProjectPackage
        from poetry.puzzle.provider import Provider
        from poetry.repositories.installed_repository import InstalledRepository
        from poetry.repositories.repository import Repository
        from poetry.utils.env import EnvManager

        plugins = self.argument("plugins")

        # Plugins should be installed in the system env to be globally available
        system_env = EnvManager.get_system_env()

        env_dir = Path(
            os.getenv("POETRY_HOME") if os.getenv("POETRY_HOME") else system_env.path
        )

        # We check for the plugins existence first.
        if env_dir.joinpath("pyproject.toml").exists():
            pyproject = tomlkit.loads(
                env_dir.joinpath("pyproject.toml").read_text(encoding="utf8")
            )
            poetry_content = pyproject["tool"]["poetry"]
            existing_packages = self.get_existing_packages_from_input(
                plugins, poetry_content, "dependencies"
            )

            if existing_packages:
                self.notify_about_existing_packages(existing_packages)

            plugins = [plugin for plugin in plugins if plugin not in existing_packages]

        if not plugins:
            return 0

        plugins = self._determine_requirements(plugins)

        # We retrieve the packages installed in the system environment.
        # We assume that this environment will be a self contained virtual environment
        # built by the official installer or by pipx.
        # If not, it might lead to side effects since other installed packages
        # might not be required by Poetry but still taken into account when resolving dependencies.
        installed_repository = InstalledRepository.load(
            system_env, with_dependencies=True
        )
        repository = Repository()

        root_package = None
        for package in installed_repository.packages:
            if package.name in Provider.UNSAFE_PACKAGES:
                continue

            if package.name == "poetry":
                root_package = ProjectPackage(package.name, package.version)
                for dependency in package.requires:
                    root_package.add_dependency(dependency)

                continue

            repository.add_package(package)

        root_package.python_versions = ".".join(
            str(v) for v in system_env.version_info[:3]
        )
        # We create a `pyproject.toml` file based on all the information
        # we have about the current environment.
        self.create_pyproject_from_package(root_package, env_dir)

        # We add the plugins to the dependencies section of the previously
        # created `pyproject.toml` file
        pyproject = tomlkit.loads(
            env_dir.joinpath("pyproject.toml").read_text(encoding="utf8")
        )
        poetry_dependency_section = pyproject["tool"]["poetry"]["dependencies"]
        plugin_names = []
        for plugin in plugins:
            if "version" in plugin:
                # Validate version constraint
                parse_constraint(plugin["version"])

            constraint = tomlkit.inline_table()
            for name, value in plugin.items():
                if name == "name":
                    continue

                constraint[name] = value

            if len(constraint) == 1 and "version" in constraint:
                constraint = constraint["version"]

            poetry_dependency_section[plugin["name"]] = constraint
            plugin_names.append(plugin["name"])

        env_dir.joinpath("pyproject.toml").write_text(
            tomlkit.dumps(pyproject), encoding="utf-8"
        )

        # From this point forward, all the logic will be deferred to
        # the update command, by using the previously created `pyproject.toml`
        # file.
        application = cast("Application", self.application)
        update_command: "UpdateCommand" = cast(
            "UpdateCommand", application.find("update")
        )
        # We won't go through the event dispatching done by the application
        # so we need to configure the command manually
        update_command.set_poetry(Factory().create_poetry(env_dir))
        update_command.set_env(system_env)
        application._configure_installer(update_command, self._io)

        argv = ["update"] + plugin_names
        if self.option("dry-run"):
            argv.append("--dry-run")

        return update_command.run(
            IO(
                StringInput(" ".join(argv)),
                self._io.output,
                self._io.error_output,
            )
        )

    def create_pyproject_from_package(
        self, package: "ProjectPackage", path: "Path"
    ) -> None:
        if path.joinpath("pyproject.toml").exists():
            return

        import tomlkit

        from poetry.layouts.layout import POETRY_DEFAULT

        pyproject = tomlkit.loads(POETRY_DEFAULT)
        content = pyproject["tool"]["poetry"]

        content["name"] = package.name
        content["version"] = package.version.text
        content["description"] = package.description
        content["authors"] = package.authors

        dependency_section = content["dependencies"]
        dependency_section["python"] = package.python_versions

        for dep in package.requires:
            constraint = tomlkit.inline_table()
            if dep.is_vcs():
                constraint[dep.vcs] = dep.source_url

                if dep.reference:
                    constraint["rev"] = dep.reference
            elif dep.is_file() or dep.is_directory():
                constraint["path"] = dep.source_url
            else:
                constraint["version"] = dep.pretty_constraint

            if not dep.marker.is_any():
                constraint["markers"] = str(dep.marker)

            if dep.extras:
                constraint["extras"] = list(sorted(dep.extras))

            if len(constraint) == 1 and "version" in constraint:
                constraint = constraint["version"]

            dependency_section[dep.name] = constraint

        path.joinpath("pyproject.toml").write_text(
            pyproject.as_string(), encoding="utf-8"
        )

    def get_existing_packages_from_input(
        self, packages: List[str], poetry_content: Dict, target_section: str
    ) -> List[str]:
        existing_packages = []

        for name in packages:
            for key in poetry_content[target_section]:
                if key.lower() == name.lower():
                    existing_packages.append(name)

        return existing_packages

    def notify_about_existing_packages(self, existing_packages: List[str]) -> None:
        self.line(
            "The following plugins are already present in the "
            "<c2>pyproject.toml</c2> file and will be skipped:\n"
        )
        for name in existing_packages:
            self.line("  • <c1>{name}</c1>".format(name=name))

        self.line(
            "\nIf you want to update it to the latest compatible version, "
            "you can use `<c2>poetry plugin update package</c2>`.\n"
            "If you prefer to upgrade it to the latest available version, "
            "you can use `<c2>poetry plugin add package@latest</c2>`.\n"
        )
