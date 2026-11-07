@echo off
echo This will add a Windows Firewall rule for MicroLoan Manager on PRIVATE networks only.
echo Run this file as Administrator on the central server PC.
pause
netsh advfirewall firewall add rule name="MicroLoan Manager TCP 5050" dir=in action=allow protocol=TCP localport=5050 profile=private
echo.
echo Firewall rule added (if Windows accepted the command).
pause
