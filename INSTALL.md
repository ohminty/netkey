# Setting up NetKey

This guide takes you from an empty memory card to an old PC playing a LAN party with your friends over the internet.
No experience needed: just do the steps in order. It takes about 30 minutes, most of it waiting.

You type exactly **one command** in the whole guide, and you can copy and paste it.

---

## What you need

| Thing | Notes |
|---|---|
| **A Raspberry Pi** | A Pi 3, 4 or 5. Older models work but can be slow and laggy. |
| **Its power supply** | The official one is the safe choice. |
| **A microSD card** | 8 GB or more. |
| **A USB network adapter** | A small plug with a network socket ("USB to Ethernet"). Your old PCs connect here. |
| **Two network cables** | One from the Pi to your router, one from the adapter to the old PC. |
| **An old PC with a network card** | Windows 95, 98, XP or DOS. |
| **A network switch** (only for 2+ old PCs) | Plug the old PCs into the switch, and the switch into the adapter. Very old network cards need a switch or hub that does 10 Mbit. |
| **Your normal computer** | Windows or Mac, for setting things up. |

> **Pi on Wi-Fi instead?** Then you don't need the USB adapter: the old PC goes into the Pi's own network socket,
> and NetKey figures that out by itself.

---

## Step 1: Put the system on the memory card

1. On your normal computer, download and install **Raspberry Pi Imager** from **raspberrypi.com/software**.
2. Put the microSD card into your computer (use a card reader if needed).
3. Open Raspberry Pi Imager and pick:
   - **Device:** your Raspberry Pi model.
   - **Operating system:** *Raspberry Pi OS (other)*, then **Raspberry Pi OS Lite** (the 64-bit one if it's offered).
   - **Storage:** your microSD card.
4. When the Imager offers to **customise** the settings, say yes and fill in:
   - **Hostname:** `netkey`
   - **Username:** `pi`, and a **password** you'll remember. Write it down.
   - **Wi-Fi:** only if the Pi will use Wi-Fi. Otherwise leave it empty.
   - **Time zone and keyboard:** yours.
   - **SSH:** turn it **on**, with **password** login.
5. Write the card. This erases everything on it. Wait until the Imager says it's finished, then take the card out.

## Step 2: Plug in the Pi

1. Put the microSD card into the Pi.
2. Connect the Pi to your **router** with a network cable (skip this if you set up Wi-Fi).
3. Plug the **USB network adapter** into the Pi.
4. Plug in the power. The Pi starts by itself. **Wait 3 minutes** the first time.

## Step 3: Connect to the Pi from your computer

You'll now open a text window on your computer and "log in" to the Pi through it.

1. Open the text window:
   - **Mac:** press `Cmd` + `Space`, type **Terminal**, press `Enter`.
   - **Windows:** right-click the Start button and choose **Terminal** (or **Windows PowerShell**).
2. Type this and press `Enter`:
   ```
   ssh pi@netkey.local
   ```
3. The first time, it asks *"Are you sure you want to continue connecting?"*. Type `yes` and press `Enter`.
4. Type the **password** from step 1 and press `Enter`.
   **Nothing shows on screen while you type it.** That's normal, just type it and press `Enter`.

You're in when the line ends with something like `pi@netkey:~ $`.

> **"Could not resolve hostname"?** The Pi may still be starting: wait a minute and try again. Still no luck? Look
> up the Pi's address in your router's list of devices (it's called *netkey*) and use that instead, for example
> `ssh pi@192.168.1.23`.

## Step 4: Install NetKey

Copy this whole line, paste it into the text window and press `Enter`:

```
curl -fsSL https://raw.githubusercontent.com/ohminty/netkey/main/get.sh | sudo bash
```

(If it asks for a password, type the one from step 1. Nothing shows while you type it.)

Lots of text scrolls past. Let it work: **5 to 15 minutes**, longer on older Pis. It's finished when you see
**Done.** and a line starting with **Web GUI:**.

You can close the text window now. You won't need it again.

## Step 5: Open NetKey in your browser

1. On your normal computer (on the same network as the Pi), open your web browser and go to:
   **https://netkey.local**
2. The browser warns that the connection *"is not private"*. That's expected: your NetKey made its own security
   certificate, and the browser doesn't know it yet. Let it through once:
   - **Chrome / Edge:** click **Advanced**, then **Continue to netkey.local**.
   - **Safari:** click **Show Details**, then **visit this website**.
   - **Firefox:** click **Advanced…**, then **Accept the Risk and Continue**.
3. Sign in with user **admin** and password **admin**.
4. NetKey asks you to choose your own password right away. Pick one and write it down.

## Step 6: Give your box a name

Click **Settings** in the menu, and fill in **Box name**, for example `Lars`. This is the name your friends
see when you join their parties.

## Step 7: Connect your old PC

1. Plug a network cable from the **USB network adapter** into the old PC's network card (or into your switch).
2. On the old PC, set the network to get its address **automatically**. In Windows 95/98:
   **Control Panel → Network → TCP/IP → Properties → Obtain an IP address automatically**.
3. Many old games use **IPX** instead. In the same Network window, click **Add → Protocol → Microsoft →
   IPX/SPX-compatible Protocol**. Windows may ask for the Windows CD.
4. Restart the old PC.
5. In NetKey, click **LAN** in the menu. Your PC shows up under **Connected PCs** within a minute.

Want the old PC on the internet too? On the **LAN** page, turn on **Share my internet with the LAN**.

## Step 8: Your first LAN party

**To host a party:**

1. Click **Parties** in the menu, then **Host a party**.
2. Choose **Private, via the cloud**. It works behind any router, with no settings to change.
3. Click **Start party**.
4. You get an **invite code** that starts with `KEY-`. Click **Copy code** and send it to your friends (text
   message, email, Discord...).
5. When a friend joins, their box appears with an **Approve** button. Click it.

**To join a friend's party:**

1. Click **Parties** in the menu.
2. Paste the code into **Join with a code** and click **Join**.
3. Wait for your friend to click **Approve**. Then you're in.

Now start a game on the old PCs and look for a network or LAN game. Your friends' PCs show up as if they were in
the same room.

---

## Updating NetKey

NetKey shows a message in the browser when a new version is out. To update, do steps 3 and 4 again (log in and paste
the same command). Your settings, password and name are kept.

## If something goes wrong

| Problem | What to do |
|---|---|
| **netkey.local doesn't open** in the browser | Use the Pi's address from your router instead, for example `https://192.168.1.23`. Your computer must be on the same network as the Pi (not a guest Wi-Fi). |
| **The install stopped with an error** | Check that the Pi is connected to the internet, then paste the command again. Running it twice is safe. |
| **The old PC doesn't show up** under Connected PCs | Check the cable and that the network card's light is on. Restart the old PC. |
| **A friend is stuck on "Waiting for the host"** | The host must click **Approve** on their Parties page. |
| **The game doesn't find the other players** | Make sure every PC uses the same protocol (IPX or TCP/IP) and that the game is set to LAN/network. |
| **Forgot the NetKey password** | Log in as in step 3 and type `sudo netkey-passreset`. The login goes back to **admin** / **admin**. |
| **"Host key verification failed"** after setting up the card again | Type `ssh-keygen -R netkey.local` on your computer, then try step 3 again. |
| **The Pi lost its Wi-Fi** | After 5 minutes it makes its own Wi-Fi called **NetKey-XXXX** (password `netkeyrescue`). Connect to it and open **http://192.168.4.1** to fix the settings. |
| **Something else** | Ask for help and include what NetKey's **Dashboard** page says. |

---

## For the technically curious

- **Two boxes in one house?** Give the second a different name: add `-s -- --hostname netkey2` at the end of the
  install command, after `sudo bash`. It's then at **https://netkey2.local**.
- **Remove NetKey:** `sudo /opt/netkey/uninstall.sh` (add `--purge-config` to also delete the settings).
- **Health report:** `sudo /opt/netkey/diagnose.sh` prints what the box sees: network ports, services and party
  status.
- **Supported systems:** Raspberry Pi OS or Debian 12 (Bookworm) or 13 (Trixie), 32 or 64 bit.
- **Without the one-liner:** download `netkey-<version>-install.sh` from the Releases page, copy it to the Pi and run
  `sudo bash netkey-<version>-install.sh`.
