// Приложение для Android. Версии зависимостей — под Expo SDK 53, как у vici.
export default {
  expo: {
    name: 'Coda',
    slug: 'music-hub',
    version: '0.1.0',
    orientation: 'portrait',
    userInterfaceStyle: 'dark',
    backgroundColor: '#141416',
    scheme: 'musichub',
    platforms: ['android'],
    android: {
      package: 'com.aut0iq.musichub',
      versionCode: 1,
      backgroundColor: '#141416',
      edgeToEdgeEnabled: true,
      // шаблон Expo добавляет их по умолчанию, приложению они не нужны
      blockedPermissions: [
        'android.permission.READ_EXTERNAL_STORAGE',
        'android.permission.WRITE_EXTERNAL_STORAGE',
        'android.permission.SYSTEM_ALERT_WINDOW',
      ],
    },
    plugins: [
      // сервер без HTTPS (порт 80/443 заняты) отдаётся по http — как и у vici
      ['expo-build-properties', { android: { usesCleartextTraffic: true, minSdkVersion: 24 } }],
      'expo-secure-store',
    ],
  },
};
