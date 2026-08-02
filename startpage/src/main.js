// Newtab entry: mount the precompiled SFC app into #app (§10). In the real page the
// store reads the browser globals (chrome / fetch); no runtime compiler is bundled.
import { createApp } from "vue";
import App from "./App.vue";

createApp(App).mount("#app");
