
.PHONY: clean

clean:
	-rm -rf build/ temp/ MANIFEST dist/ src/*.egg-info radical/pilot/VERSION pylint.out *.egg examples/result-*.dat
	find . -name \*.pyc -exec rm -f {} \;
	find . -name \*.egg-info -exec rm -rf {} \;
