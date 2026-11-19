PYTHON ?= python3

.PHONY: offline-parking-tests
offline-parking-tests:
	$(PYTHON) -B -m unittest test_parking_booking
