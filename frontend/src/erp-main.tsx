import "./erp.css";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { Erp } from "./Erp";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Erp />
  </StrictMode>,
);
