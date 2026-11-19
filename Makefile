PYTHON ?= python3

.PHONY: offline-parking-tests offline-release-tests offline-renewal-tests offline-release-trace
offline-parking-tests:
	$(PYTHON) -B -m unittest test_parking_booking

offline-release-tests:
	$(PYTHON) -B -m unittest test_parking_release

offline-renewal-tests:
	$(PYTHON) -B -m unittest test_parking_renewal

offline-release-trace:
	BOOKKER_OFFLINE_TRACE=1 $(PYTHON) -B -m unittest \
		test_parking_release.ScheduledEntryPointTests.test_scheduled_form_submits_at_first_verified_control \
		test_parking_release.ScheduledEntryPointTests.test_concurrent_trigger_and_ambiguous_restart_reconcile_without_another_click \
		test_parking_renewal.MobileEntryPointTests
