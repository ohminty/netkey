NETKEY SHARED FOLDER
====================

Files you upload on the NetKey web page (Fileshare) land here, and every PC
on the retro LAN can get them right away, three ways:

WINDOWS (Windows for Workgroups 3.11, 95, 98, ME, XP)
  Open Network Neighborhood, then NETKEY, then FILES. No password.
  Or in a DOS box:  NET USE F: \\NETKEY\FILES
  The PC needs TCP/IP (DHCP) and "Client for Microsoft Networks".
  If NETKEY doesn't show up: Start > Find > Computer > NETKEY, or use the
  box's address instead of the name:  \\10.99.0.1\FILES

FTP (DOS, Windows, anything with an FTP program)
  ftp 10.99.0.1     (or: ftp netkey)
  Log in with a modem user's name and password (the Users page).
  On DOS, mTCP's FTP client works well with a packet driver.

WEB (any browser, even Mosaic or Netscape 1)
  http://10.99.0.1/     (or: http://netkey/)

10.99.0.1 is the box's address on the retro LAN (the PCs' gateway). If you
changed the LAN subnet, or the box has another name, use those instead.

You can delete this file.
