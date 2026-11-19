PYTHON ?= python3

.PHONY: offline-parking-tests offline-release-tests offline-release-trace
offline-parking-tests:
	$(PYTHON) -B -m unittest test_parking_booking

offline-release-tests:
	$(PYTHON) -B -m unittest test_parking_release

offline-release-trace:
	BOOKKER_OFFLINE_TRACE=1 $(PYTHON) -B -m unittest \
		test_parking_release.ScheduledEntryPointTests.test_one_context_prepare_release_submit_verify_and_exit
