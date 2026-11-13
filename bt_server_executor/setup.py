from setuptools import find_packages, setup

package_name = "bt_server_executor"

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
    description="Behavior Tree Server executor: the rclpy node that runs behaviour trees, and run_tree.",
    license="MIT",
    tests_require=["pytest"],
    entry_points={
        "console_scripts": [
            "executor = bt_server_executor.node:main",
            "run_tree = bt_server_executor.run_tree:main",
        ],
    },
)
