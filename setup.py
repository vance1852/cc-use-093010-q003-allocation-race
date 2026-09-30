"""本地安装入口。"""

from setuptools import find_packages, setup


setup(
    name="trade-flow-operations",
    version="0.1.0",
    description="全球数字贸易合作资源、证据评估与统计质量服务",
    long_description=open("README.md", encoding="utf-8").read(),
    long_description_content_type="text/markdown",
    package_dir={"": "src"},
    packages=find_packages("src"),
    python_requires=">=3.11",
)
