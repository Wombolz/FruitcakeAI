# Release Notes v0.7.30

## Summary

This release adds deterministic system-job tasks for trusted maintenance workflows that should run through Fruitcake's task scheduler and inspection surfaces without invoking an LLM agent loop.

## Included Changes

- added a `system_job` execution profile for direct, non-LLM task execution
- added system-job handlers for nightly memory extraction and RSS cache refresh
- normalized task creation paths so explicit system jobs and refresh-RSS maintenance tasks resolve to the deterministic profile
- preserved ordinary task/run records and inspection behavior while avoiding unnecessary model calls for maintenance jobs
- added focused regression coverage for system-job profile resolution, task API creation, direct task execution, memory extraction, and RSS refresh behavior

## Notes

- focused verification passed before release:
  - `.venv/bin/pytest tests/test_task_profiles.py::test_system_job_profile_runs_nightly_memory_extraction_directly tests/test_task_profiles.py::test_system_job_profile_runs_refresh_rss_cache_directly tests/test_task_steps.py::test_system_job_task_runs_without_llm_agent_loop tests/test_task_steps.py::test_refresh_rss_cache_system_job_runs_without_llm_agent_loop tests/test_tasks_api.py::test_create_task_accepts_explicit_system_job_recipe_family tests/test_tasks_api.py::test_create_task_accepts_refresh_rss_cache_as_system_job tests/test_profile_resolver.py tests/test_agent.py::test_create_task_tool_normalizes_refresh_rss_cache_to_system_job tests/test_memory_extraction.py -q`
  - `git diff --check`
- focused result count for the merged system-job verification set: `19 passed`
- one broader legacy topic-watcher test remains unrelated to this release and appears sensitive to date-window assumptions in older memory-history fixtures
