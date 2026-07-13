import { useEffect } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import AppShell from "./components/AppShell";
import DashboardPage from "./pages/DashboardPage";
import LogsPage from "./pages/LogsPage";
import SettingsPage from "./pages/SettingsPage";

export default function App() {
  useEffect(() => {
    document.documentElement.classList.add("dark");
    document.documentElement.style.colorScheme = "dark";
  }, []);

  return (
    <BrowserRouter>
      <AppShell>
        <Routes>
          <Route element={<DashboardPage />} path="/" />
          <Route element={<SettingsPage />} path="/settings" />
          <Route element={<LogsPage />} path="/logs" />
          <Route element={<Navigate replace to="/" />} path="*" />
        </Routes>
      </AppShell>
    </BrowserRouter>
  );
}
