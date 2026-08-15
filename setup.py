from setuptools import setup, find_packages

setup(
    name='househunt',
    packages=find_packages(exclude=['tests', 'tests.*']),
    package_data={'househunt': []},
    install_requires=[
        'requests',
    ],
    extras_require={
        # Only needed by the listing cache.
        'cache': ['tinydb'],
    },
    python_requires='>=3.10',
    version='0.7.0',
    description=(
        'Rank NJ ZIP codes by rail/bus commute to Midtown Manhattan, '
        'then search listings in the best ones'
    ),
    author='AlThor880',
    author_email='althor880@gmail.com',
    url='https://github.com/nikunjangc/househunt',
    keywords=['house', 'realty', 'commute', 'gtfs', 'raptor', 'transit', 'njmls'],
    entry_points={
        'console_scripts': [
            'househunt=househunt.cli:main',
        ],
    },
    classifiers=[
        'Programming Language :: Python :: 3',
    ],
)
