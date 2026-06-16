# Installing FastStack on macOS

FastStack for macOS is distributed as a zip file containing the FastStack app.
It is not notarized by Apple, so macOS will warn you the first time you open it.
Only continue if you got the file from the official FastStack release page or
directly from someone you trust.

Official releases:
https://github.com/AlanRockefeller/faststack/releases

## Which File Should I Download?

There are two Mac downloads:

- `FastStack-macos-arm64.zip` for Macs with Apple silicon, such as M1, M2, M3,
  M4, or newer.
- `FastStack-macos-x64.zip` for older Macs with an Intel processor.

To check which Mac you have:

1. Click the Apple menu in the top-left corner of your screen.
2. Click `About This Mac`.
3. Look for `Chip` or `Processor`.
4. If it says `Apple M1`, `Apple M2`, `Apple M3`, `Apple M4`, or similar,
   download `FastStack-macos-arm64.zip`.
5. If it says `Intel`, download `FastStack-macos-x64.zip`.

## Install FastStack

1. Download the correct Mac zip file from the FastStack release page.
2. Open your `Downloads` folder.
3. Double-click the downloaded zip file.
4. A file named `FastStack.app` should appear.
5. Open a second Finder window.
6. Click `Applications` in the Finder sidebar.
7. Drag `FastStack.app` into the `Applications` folder.

FastStack is now installed.

## Open FastStack for the First Time

1. Open the `Applications` folder.
2. Double-click `FastStack`.
3. macOS may show a message saying FastStack cannot be opened because Apple
   cannot check it, or because it is from an unknown developer.
4. Click `Done` or `Cancel`. This first attempt is expected.
5. Open the Apple menu in the top-left corner of the screen.
6. Click `System Settings`.
7. Click `Privacy & Security` in the sidebar. You may need to scroll down.
8. Scroll to the `Security` section.
9. Look for a message about `FastStack` being blocked.
10. Click `Open Anyway`.
11. Enter your Mac login password if asked.
12. Click `Open`.

After this, macOS remembers your choice. In the future, you can open FastStack
normally by double-clicking it in the `Applications` folder.

## If You Do Not See `Open Anyway`

The `Open Anyway` button is only available for a limited time after you try to
open the app.

1. Go back to the `Applications` folder.
2. Double-click `FastStack` again.
3. Click `Done` or `Cancel` if the warning appears.
4. Go back to `System Settings` > `Privacy & Security`.
5. Check the `Security` section again.

## If macOS Says the App Is Damaged

Try these steps:

1. Delete the copied `FastStack.app` from the `Applications` folder.
2. Delete the downloaded zip file.
3. Download the zip file again from the official FastStack release page.
4. Double-click the zip file to unzip it.
5. Drag the new `FastStack.app` to `Applications`.
6. Try the first-open steps again.

Do not install FastStack if you do not trust where the file came from.

## Start Using FastStack

When FastStack opens, choose the folder that contains the photos you want to
review. FastStack is designed for folders containing JPG files, and it can pair
matching RAW files automatically when they are in the same folder.

To keep FastStack in the Dock:

1. Open FastStack.
2. Control-click the FastStack icon in the Dock.
3. Click `Options`.
4. Click `Keep in Dock`.

## Updating FastStack

1. Quit FastStack if it is open.
2. Download the newer Mac zip file from the FastStack release page.
3. Double-click the zip file.
4. Drag the new `FastStack.app` into `Applications`.
5. If Finder asks whether to replace the old app, click `Replace`.
6. Open FastStack again.

## Uninstalling FastStack

1. Quit FastStack if it is open.
2. Open the `Applications` folder.
3. Drag `FastStack.app` to the Trash.

FastStack settings may remain in your user account so they can be reused if you
install FastStack again later.
