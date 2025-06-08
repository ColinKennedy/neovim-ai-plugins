.PHONY: black check-black check-isort generate_readme isort mypy

black:
	python -m black generate_readme.py

check-black:
	python -m black --diff --check generate_readme.py

check-isort:
	python -m isort --profile black --verbose --check-only --diff generate_readme.py

generate_readme:
	python generate_readme.py

isort:
	python -m isort --profile black generate_readme.py

mypy:
	python -m mypy --strict generate_readme.py
