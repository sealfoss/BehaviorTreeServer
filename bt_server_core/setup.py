from setuptools import find_packages, setup

package_name = "bt_server_core"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Behavior Tree Server maintainers",
    maintainer_email="noreply@example.com",
    description="Behavior Tree Server core: document model, engine, blackboard, leaf base classes.",
    license="MIT",
    tests_require=["pytest"],
)
