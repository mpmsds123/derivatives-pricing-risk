.PHONY: install test run
install:
	pip3 install -r requirements.txt
test:
	python3 -m pytest -q
run:
	cd curves && python3 run.py
	cd surface && python3 run.py
	cd attribution && python3 run.py
	python3 risk/run_risk.py
