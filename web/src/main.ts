import { mount } from 'svelte';
import App from './App.svelte';
import './app.css';
import { session } from './lib/stores/session.svelte';

session.connect();

export default mount(App, { target: document.getElementById('app')! });
