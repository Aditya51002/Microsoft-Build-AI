const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";

export const apiBaseUrl = API_BASE_URL.replace(/\/$/, "");

export const wsBaseUrl = apiBaseUrl.replace(/^http/, "ws");
