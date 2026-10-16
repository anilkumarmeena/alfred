// A connection's server address: lib/address.ts, as the Connections page checks it. Run: make test (or npm test in web/)
import assert from "node:assert/strict";
import { test } from "vitest";
import { badAddress, hostOf } from "../src/lib/address";

test("a server address is an https URL, or empty for the default", () => {
  for (const url of ["", "https://git.example.com/api/v4", "https://ghe.example.com", "http://localhost:8080/api"]) {
    assert.equal(badAddress(url), "", url);
  }
  for (const url of ["http://git.example.com/api/v4", "git.example.com", "https://", "ftp://git.example.com",
    "https://user:pw@git.example.com", "https://git.example.com/?x=1", "https://git example.com", "https:git.example.com"]) {
    assert.match(badAddress(url), /https:\/\//, url);
  }
});

test("a path change keeps the host, so the keys stay", () => {
  assert.equal(hostOf("https://git.example.com/api/v4"), hostOf("https://GIT.example.com/api/v4/"));
  assert.notEqual(hostOf("https://gitlab.com/api/v4"), hostOf("https://git.example.com/api/v4"));
  assert.notEqual(hostOf(""), hostOf("https://gitlab.com/api/v4"));
});
