// Приложение для Android. Версии зависимостей — под Expo SDK 53, как у vici.
export default {
  expo: {
    name: 'Coda',
    slug: 'music-hub',
    version: '0.1.2',
    orientation: 'portrait',
    userInterfaceStyle: 'dark',
    backgroundColor: '#141416',
    scheme: 'musichub',
    // иконка в стиле vici: золотая антиква и светящаяся дуга (картинки делает scripts/make-icon.py)
    icon: './assets/icon.png',
    platforms: ['android'],
    android: {
      package: 'com.aut0iq.musichub',
      versionCode: 3,
      backgroundColor: '#141416',
      adaptiveIcon: {
        foregroundImage: './assets/adaptive-icon.png',
        monochromeImage: './assets/monochrome-icon.png',   // тематические иконки Android 13+
        backgroundColor: '#0E0A0F',
      },
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
