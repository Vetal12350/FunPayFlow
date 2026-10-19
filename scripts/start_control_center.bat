@echo off
py -3.13 "%~dp0control_center.py"
if errorlevel 1 pause
