"""Automation Studio orchestrator: workflow registry, run manager and HTTP API.

Runs as the local backend (a sidecar the Electron app launches, and a standalone
process agents/scripts can run on their own). Owns all run state and persistence.
"""
