# OneSignal setup

## Backend env

Add these variables to `.env`:

```env
ONESIGNAL_APP_ID=your-onesignal-app-id
ONESIGNAL_REST_API_KEY=your-onesignal-rest-api-key
ONESIGNAL_API_URL=https://api.onesignal.com
ONESIGNAL_TARGET_CHANNEL=push
ONESIGNAL_TIMEOUT_SECONDS=10
```

Run migrations:

```bash
uv run python manage.py migrate
```

## Frontend/mobile flow

Use the OneSignal SDK in the client app and log in with the backend user UUID after authentication:

```ts
await OneSignal.login(user.uuid)
```

That makes the backend able to target the user through OneSignal `external_id`.

### Android

- Configure Firebase Cloud Messaging in OneSignal.
- Add the OneSignal Android SDK in the mobile app.
- Request notification permission where applicable.
- After login, call `OneSignal.login(user.uuid)`.

### iOS

- Configure APNs key/certificate in OneSignal.
- Enable Push Notifications and Background Modes in Xcode.
- Add the OneSignal iOS SDK in the mobile app.
- After login, call `OneSignal.login(user.uuid)`.

## Notes

- The backend payload is the same for Android and iOS. Delivery is resolved by OneSignal through FCM/APNs.
- The app only needs to associate the logged-in user with OneSignal using `OneSignal.login(user.uuid)`.
- The backend sends notifications directly to that `external_id`, for example in the ambassador job approval flow.
