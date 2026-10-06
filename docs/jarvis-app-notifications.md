# jarvis-app: the native side of the notifications switch

The page's Notifications switch already works with today's iPhone app build: it reads and asks for permission
with the plugin's `checkPermissions` / `requestPermissions`, and opens iOS Settings by navigating to
`app-settings:` (Capacitor's `WebViewDelegationHandler` hands a top-level link it doesn't serve to
`UIApplication.shared.open`). Two gaps remain, which these jarvis-app changes close; the page detects both new
methods and uses them when the build has them:

- a newly allowed iPhone only sends its APNs token to the Mac at the next session start;
- Settings opens on JARVIS's page rather than straight on its notification settings.

Xcode isn't on the Mac that wrote this, so none of it has been built.

## 1. `packages/jarvis-audio/ios/Sources/JarvisAudioPlugin/PushNotifications.swift`

Make `register` async and return the answer, instead of wrapping its body in `Task { }`:

```swift
/// Asks for permission the first time, then for the APNs token, which goes to the server once it comes.
@discardableResult
func register(server: ServerAddress, passcode: String, device: String) async -> Bool {
    install()
    target = Target(server: server, passcode: passcode, device: device)
    let center = UNUserNotificationCenter.current()
    do {
        guard try await center.requestAuthorization(options: [.alert, .sound, .badge]) else {
            NSLog("JarvisAudio: notifications are turned off for JARVIS in Settings")
            return false
        }
    } catch {
        NSLog("JarvisAudio: notification permission: \(error)")
        return false
    }
    // Every time, as Apple asks: the token can change, and iOS answers quickly with the current one.
    UIApplication.shared.registerForRemoteNotifications()
    return true
}
```

## 2. `packages/jarvis-audio/ios/Sources/JarvisAudioPlugin/JarvisAudioPlugin.swift`

- `import UIKit` at the top.
- Add to `pluginMethods`:
  ```swift
  CAPPluginMethod(name: "registerNotifications", returnType: CAPPluginReturnPromise),
  CAPPluginMethod(name: "openSettings", returnType: CAPPluginReturnPromise),
  ```
- In `start`, the call becomes `await PushNotifications.shared.register(server: server, passcode: passcode, device: device)`
  (it's already inside `Task { @MainActor in … }`). A refusal there stays a log line: no alert at session start.
- New methods, under `// MARK: - Permissions`:
  ```swift
  /// The page's notifications switch: asks iOS (a dialog only the first time) and, when allowed, hands the APNs
  /// token to the Mac now rather than at the next session start.
  @objc func registerNotifications(_ call: CAPPluginCall) {
      guard let passcode = call.getString("passcode"), !passcode.isEmpty else {
          return call.reject("Enter your passcode first.", "UNAUTHORIZED")
      }
      let given = call.getString("serverUrl").flatMap { try? ServerAddress($0) }
      guard let server = given ?? ServerSettings.current else {
          return call.reject("JARVIS's address isn't set.", "NO_SERVER")
      }
      let device = call.getString("device").flatMap { $0.isEmpty ? nil : $0 } ?? "iphone"
      Task { @MainActor in
          await PushNotifications.shared.register(server: server, passcode: passcode, device: device)
          call.resolve(["notifications": await Self.notificationState()])
      }
  }

  /// JARVIS's notification settings in the Settings app (iOS 16+), else its page there: the only place
  /// notifications can be allowed again once refused, or turned off.
  @objc func openSettings(_ call: CAPPluginCall) {
      Task { @MainActor in
          let link: String
          if #available(iOS 16.0, *) { link = UIApplication.openNotificationSettingsURLString } else { link = UIApplication.openSettingsURLString }
          guard let url = URL(string: link) else { return call.reject("No link to Settings.") }
          UIApplication.shared.open(url) { opened in call.resolve(["opened": opened]) }
      }
  }
  ```
- `requestPermissions`: honour Capacitor's `permissions` option, so the page's `{ permissions: ["notifications"] }`
  doesn't ask for the mic too:
  ```swift
  @objc override public func requestPermissions(_ call: CAPPluginCall) {
      let asked = call.getArray("permissions", String.self) ?? ["microphone", "notifications"]
      Task {
          if asked.contains("microphone") { _ = await AVAudioApplication.requestRecordPermission() }
          if asked.contains("notifications") {
              _ = try? await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge])
          }
          call.resolve(["microphone": Self.micState, "notifications": await Self.notificationState()])
      }
  }
  ```

## 3. `packages/jarvis-audio/src/definitions.ts` and `web.ts`

```ts
/**
 * iOS: asks for notification permission (a dialog only the first time) and, when allowed, registers the app's
 * APNs token with /api/push/native now. Rejects with UNAUTHORIZED or NO_SERVER like start().
 */
registerNotifications(options: { serverUrl?: string; passcode: string; device?: string }): Promise<{ notifications: PermissionState }>;
/** iOS: opens JARVIS's notification settings in the Settings app. */
openSettings(): Promise<{ opened: boolean }>;
```

In `web.ts` both `throw this.unimplemented(…)`. Android is optional (the page only uses these on iOS):
`openSettings` there would start `Settings.ACTION_APP_NOTIFICATION_SETTINGS` with `EXTRA_APP_PACKAGE`.

## 4. Keep the page's copy in step

- `web-hook/native.ts`: copy jarvis's `web/src/lib/native.ts` (it adds `nativePlatform`, `appNotifications`,
  `APP_SETTINGS`, and the optional `checkPermissions`, `requestPermissions`, `registerNotifications` and
  `openSettings` on `NativeAudio`).
- `tests/native.test.ts`: copy jarvis's `web/tests/native.test.ts`.
- `web-hook/README.md`: the `push.ts` row no longer holds: in the app, the switch asks iOS through the plugin and
  offers Open Settings once refused, instead of `pushBlocked()` explaining.

## 5. Checking on the phone

1. Fresh install: Settings → Notifications, switch on → iOS's dialog → Allow → the switch stays on, a toast over
   the panel says they're on, and the iPhone shows under Devices (straight away with this change, after the next
   session start without it).
2. Turn them off in iOS Settings and come back: the switch is off and a "Blocked in iOS Settings" row offers
   Open Settings. Tapping the switch shows a toast over the panel with the same button.
3. Tapping the switch while on: a toast explains they're turned off in iOS Settings, with Open Settings.
