// Список серверов (адреса, без секретов) — в AsyncStorage; токен и пароль Navidrome — в SecureStore.
import AsyncStorage from '@react-native-async-storage/async-storage';
import * as SecureStore from 'expo-secure-store';

const LIST_KEY = 'hub.servers.v1';
const LAST_KEY = 'hub.last.v1';
const secretKey = (id) => `hub.secret.${id}`;

export async function listServers() {
  try {
    const raw = await AsyncStorage.getItem(LIST_KEY);
    const list = raw ? JSON.parse(raw) : [];
    return Array.isArray(list) ? list : [];
  } catch { return []; }
}

export async function saveServer(server, secrets) {
  const list = await listServers();
  const i = list.findIndex((s) => s.id === server.id);
  if (i >= 0) list[i] = { ...list[i], ...server }; else list.push(server);
  await AsyncStorage.setItem(LIST_KEY, JSON.stringify(list));
  if (secrets) await SecureStore.setItemAsync(secretKey(server.id), JSON.stringify(secrets));
}

export async function loadSecrets(id) {
  try {
    const raw = await SecureStore.getItemAsync(secretKey(id));
    return raw ? JSON.parse(raw) : null;
  } catch { return null; }
}

export async function removeServer(id) {
  const list = (await listServers()).filter((s) => s.id !== id);
  await AsyncStorage.setItem(LIST_KEY, JSON.stringify(list));
  try { await SecureStore.deleteItemAsync(secretKey(id)); } catch { /* ключа могло не быть */ }
  if ((await getLast()) === id) await AsyncStorage.removeItem(LAST_KEY);
}

export const getLast = async () => { try { return await AsyncStorage.getItem(LAST_KEY); } catch { return null; } };
export const setLast = async (id) => { try { await AsyncStorage.setItem(LAST_KEY, id); } catch { /* не критично */ } };

export const newId = () => `s${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
