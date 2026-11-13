from glob import glob

from setuptools import find_packages, setup

package_name = "bt_server_bringup"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
        ("share/" + package_name + "/launch", glob("launch/*.launch.py")),
        ("share/" + package_name + "/config", glob("config/*")),
        ("share/" + package_name + "/systemd", glob("systemd/*")),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Behavior Tree Server maintainers",
    maintainer_email="noreply@example.com",
    description="Behavior Tree Server: launch file, parameter files and systemd unit templates.",
    license="MIT",
)
