import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./style.css";
import { type LabData, loadLab } from "./Lab";

const root = document.getElementById("root");
async function mount() {
  if (!root) return;
  let lab: LabData | undefined;
  if (document.getElementById("parquetscope-lab")) {
    root.textContent = "Loading three measured captures…";
    try {
      lab = await loadLab();
    } catch (error) {
      root.setAttribute("role", "alert");
      root.textContent = `Cannot open real-data lab: ${String(error)}`;
      return;
    }
  }
  createRoot(root).render(
    <StrictMode>
      <App lab={lab} />
    </StrictMode>,
  );
}
void mount();
