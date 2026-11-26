import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { reactive } from "vue";
import ProductionDashboard from "./ProductionDashboard.vue";
import { createProductionScope, productionScopeKey } from "../../composables/productionScope";

const db = vi.hoisted(() => ({ getDocList: vi.fn(), getCount: vi.fn(), list: vi.fn() }));
vi.mock("frappe-js-sdk", () => ({ FrappeApp: class { db() { return db; } call() { return { get: db.list, post: async () => ({ message: {} }) }; } } }));

beforeEach(() => {
  vi.resetAllMocks();
  sessionStorage.clear();
  localStorage.clear();
  db.getCount.mockResolvedValue(0);
});

function mountDashboard(scope) {
  return mount(ProductionDashboard, {
    global: { provide: { [productionScopeKey]: scope } },
  });
}

describe("Mosaic scope", () => {
  it("restores dashboard choices and replaces a branch outside the selected company", async () => {
    sessionStorage.setItem("ury_active_company_id", "URY UAE");
    sessionStorage.setItem("ury_active_branch_id", "URY");
    db.getDocList.mockResolvedValueOnce([{ name: "URY IN" }, { name: "URY UAE" }])
      .mockResolvedValueOnce([{ name: "URY Barsha" }, { name: "URY JVC" }]);
    const { scope, initialize } = createProductionScope();
    await initialize();
    expect(scope.company).toBe("URY UAE");
    expect(scope.branch).toBe("URY Barsha");
    expect(db.getDocList).toHaveBeenLastCalledWith("Branch", expect.objectContaining({
      filters: [["company", "=", "URY UAE"]], limit: 0,
    }));
    expect(sessionStorage.getItem("ury_active_branch_id")).toBe("URY Barsha");
    expect(scope.ready).toBe(true);
  });

  it("defaults to a concrete company and branch and refreshes branches on company change", async () => {
    db.getDocList.mockResolvedValueOnce([{ name: "URY IN" }, { name: "URY UAE" }])
      .mockResolvedValueOnce([{ name: "URY" }])
      .mockResolvedValueOnce([{ name: "URY JVC" }]);
    const { scope, initialize } = createProductionScope();
    await initialize();
    expect(scope.company).toBe("URY IN");
    expect(scope.branch).toBe("URY");
    scope.company = "URY UAE";
    expect(scope.ready).toBe(false);
    await flushPromises();
    expect(scope.branch).toBe("URY JVC");
    expect(scope.ready).toBe(true);
  });

  it("keeps a branch fetch failure from opening an unfiltered dashboard", async () => {
    db.getDocList.mockResolvedValueOnce([{ name: "URY IN" }]).mockRejectedValueOnce(new Error("Forbidden"));
    const { scope, initialize } = createProductionScope();
    await initialize();
    expect(scope.ready).toBe(false);
    expect(scope.error).toContain("Unable to load branches");
  });
});

describe("ProductionDashboard filtering", () => {
  it("filters by company and branch, then permits all branches within that company", async () => {
    const scope = reactive({ ready: true, company: "URY UAE", branch: "URY JVC", loading: false });
    db.list.mockResolvedValue({ message: [{ name: "Kitchen JVC", enabled: 1 }, { name: "Bar JVC", enabled: 0 }] });
    const wrapper = mountDashboard(scope);
    await flushPromises();
    db.list.mockResolvedValue({ message: [{ name: "Legacy Kitchen", enabled: 1 }] });
    scope.branch = "all";
    await flushPromises();
    expect(db.list).toHaveBeenLastCalledWith("ury.ury.api.ury_kot_display.get_production_units", {
      company: "URY UAE", branch: null,
    });
    expect(wrapper.text()).toContain("Legacy Kitchen");
    wrapper.unmount();
  });

  it("ignores late results from the previous branch", async () => {
    let finishOldRequest;
    db.list.mockReturnValueOnce(new Promise((resolve) => { finishOldRequest = resolve; }))
      .mockResolvedValueOnce({ message: [{ name: "Kitchen Barsha", enabled: 1 }] });
    const scope = reactive({ ready: true, company: "URY UAE", branch: "URY JVC", loading: false });
    const wrapper = mountDashboard(scope);
    scope.branch = "URY Barsha";
    await flushPromises();
    finishOldRequest({ message: [{ name: "Kitchen JVC", enabled: 1 }] });
    await flushPromises();
    expect(wrapper.text()).toContain("Kitchen Barsha");
    expect(wrapper.text()).not.toContain("Kitchen JVC");
    wrapper.unmount();
  });

  it("shows an empty state for a scope with no units", async () => {
    db.list.mockResolvedValue({ message: [] });
    const wrapper = mountDashboard(reactive({ ready: true, company: "all", branch: "all", loading: false }));
    await flushPromises();
    expect(db.list).toHaveBeenLastCalledWith("ury.ury.api.ury_kot_display.get_production_units", { company: null, branch: null });
    expect(wrapper.text()).toContain("No production units");
    wrapper.unmount();
  });
});
