import os

from setuptools import find_namespace_packages, setup

package_name = "bt_server_gateway"


def static_files() -> list[str]:
    """Every file of the built page under ``bt_server_gateway/static``, relative to the package
    folder, so that the installed package serves the page (design 11.1)."""
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), package_name)
    found = []
    for folder, _, names in os.walk(os.path.join(base, "static")):
        for name in names:
            found.append(os.path.relpath(os.path.join(folder, name), base).replace(os.sep, "/"))
    return found


setup(
    name=package_name,
    version="0.1.0",
    # Namespace discovery so that the page's folders (static/, static/assets/) count as part of
    # the package (they hold package data only), as setuptools asks.
    packages=find_namespace_packages(include=[package_name, package_name + ".*"]),
    package_data={package_name: static_files()},
    include_package_data=True,
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
    ],
    install_requires=["setuptools"],
    zip_safe=False,
    maintainer="Behavior Tree Server maintainers",
    maintainer_email="noreply@example.com",
    description="Behavior Tree Server gateway: documents, browser sessions, executor links.",
    license="MIT",
    tests_require=["pytest"],
    entry_points={"console_scripts": ["gateway = bt_server_gateway.__main__:main"]},
)
