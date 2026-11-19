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
		test_parking_release.ScheduledEntryPointTests.test_rerendered_cached_rows_are_ignored_until_the_search_completes \
		test_parking_release.ScheduledEntryPointTests.test_reload_confirms_the_saved_full_day_from_contiguous_shifts \
		test_parking_release.ScheduledEntryPointTests.test_separate_confirmation_dispatches_as_soon_as_enabled \
		test_parking_release.ScheduledEntryPointTests.test_concurrent_trigger_and_ambiguous_restart_reconcile_without_another_click \
		test_parking_renewal.MobileEntryPointTests
