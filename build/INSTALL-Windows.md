# Installing FastStack on Windows

FastStack for Windows is distributed as a zip file. It is not signed with a
Microsoft-recognized publisher certificate, so Windows may show a security
warning the first time you open it. Only continue if you got the file from the
official FastStack release page or directly from someone you trust.

Official releases:
https://github.com/AlanRockefeller/faststack/releases

## What to Download

Download this file:

`FastStack-windows-x64.zip`

This is the Windows 64-bit build. Most modern Windows computers are 64-bit.

## Install FastStack

FastStack does not use a setup wizard. You install it by extracting the zip file
and keeping the extracted FastStack folder.

1. Download `FastStack-windows-x64.zip` from the FastStack release page.
2. Open your `Downloads` folder.
3. Right-click `FastStack-windows-x64.zip`.
4. Click `Extract All`.
5. Click `Extract`.
6. When extraction finishes, open the extracted folder.
7. Open the `FastStack` folder inside it.
8. Double-click `FastStack.exe`.

Important: keep `FastStack.exe` inside the `FastStack` folder. The other files
in that folder are required. If you move only `FastStack.exe` somewhere else,
the app may not start.

## If Windows Shows "Windows Protected Your PC"

Windows Defender SmartScreen may appear because this build is not signed.

1. Read the warning carefully.
2. If you trust where the file came from, click `More info`.
3. Click `Run anyway`.

FastStack should open after that.

## If Windows Shows a Smart App Control Block

Some Windows 11 computers have Smart App Control enabled. Smart App Control may
block unsigned apps and may not offer a `Run anyway` button.

If this happens:

1. Confirm that you downloaded FastStack from the official FastStack release
   page.
2. Open the Start menu.
3. Type `Windows Security`.
4. Open `Windows Security`.
5. Click `App & browser control`.
6. Click `Smart App Control settings`.
7. If Smart App Control is blocking FastStack and you choose to continue, turn
   Smart App Control off.
8. Open FastStack again.

Recent Windows versions allow Smart App Control to be turned back on after
installation, but this depends on your Windows version and organization policy.
If this is a work, school, or managed computer, you may need help from your IT
administrator.

Do not turn off Windows security features for software you do not trust.

## Make a Desktop Shortcut

If FastStack opened correctly, make a shortcut so it is easier to launch later:

1. Go back to the `FastStack` folder.
2. Right-click `FastStack.exe`.
3. Click `Show more options` if you see it.
4. Click `Send to`.
5. Click `Desktop (create shortcut)`.

Use the desktop shortcut to open FastStack in the future.

Do not move `FastStack.exe` by itself to the desktop. Make a shortcut instead.

## Start Using FastStack

When FastStack opens, choose the folder that contains the photos you want to
review. FastStack is designed for folders containing JPG files, and it can pair
matching RAW files automatically when they are in the same folder.

## Updating FastStack

1. Quit FastStack if it is open.
2. Download the newer `FastStack-windows-x64.zip` file from the FastStack
   release page.
3. Right-click the new zip file.
4. Click `Extract All`.
5. Extract it to a new folder.
6. Open the new `FastStack` folder.
7. Double-click `FastStack.exe`.
8. If the new version opens correctly, you can delete the old extracted
   FastStack folder.
9. If you made a desktop shortcut before, delete the old shortcut and create a
   new one from the new `FastStack.exe`.

## Uninstalling FastStack

1. Quit FastStack if it is open.
2. Delete the extracted `FastStack` folder.
3. Delete any desktop shortcut you created.

FastStack settings may remain in your Windows user account so they can be reused
if you install FastStack again later.
