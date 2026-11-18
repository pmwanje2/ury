// The till's category rail must follow the menu it shows. A category list
// cached in sessionStorage before the menu had courses must not hide them.
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { usePOSStore } from '../src/store/pos-store';
import { getMenuCourses } from '../src/lib/menu-course-api';
import { getRestaurantMenu } from '../src/lib/menu-api';

vi.mock('../src/lib/menu-course-api', () => ({ getMenuCourses: vi.fn() }));
vi.mock('../src/lib/menu-api', () => ({ getRestaurantMenu: vi.fn(async () => []), getAggregatorMenu: vi.fn(async () => []) }));

const courses = [
  { name: 'Mains', label: 'Mains', icon: null },
  { name: 'Spirits', label: 'Spirits', icon: null },
];

beforeEach(() => {
  sessionStorage.clear();
  vi.mocked(getMenuCourses).mockReset();
  usePOSStore.setState({ categories: [], categoriesDerived: false, menuItems: [], selectedCategory: '' });
});
afterEach(() => sessionStorage.clear());

it('refetches categories when an empty list was cached earlier', async () => {
  sessionStorage.setItem('menuCategories', '[]');
  vi.mocked(getMenuCourses).mockResolvedValue(courses as never);
  await usePOSStore.getState().fetchCategories();
  expect(usePOSStore.getState().categories.map((c) => c.name)).toEqual(['Mains', 'Spirits']);
});

it('refetches categories when a stale non-empty list was cached earlier', async () => {
  sessionStorage.setItem('menuCategories', JSON.stringify([{ name: 'Old', label: 'Old' }]));
  vi.mocked(getMenuCourses).mockResolvedValue(courses as never);
  await usePOSStore.getState().fetchCategories();
  expect(usePOSStore.getState().categories.map((c) => c.name)).toEqual(['Mains', 'Spirits']);
});

it('falls back to the courses on the loaded menu items when the course list is empty', async () => {
  vi.mocked(getMenuCourses).mockResolvedValue([]);
  usePOSStore.setState({
    menuItems: [
      { id: 'A', name: 'Rib-Eye', course: 'Mains', course_label: 'Mains' },
      { id: 'B', name: 'Gin', course: 'Spirits', course_label: 'Spirits' },
      { id: 'C', name: 'Steak', course: 'Mains', course_label: 'Mains' },
      { id: 'D', name: 'Water', course: '' },
    ] as never,
  });
  await usePOSStore.getState().fetchCategories();
  expect(usePOSStore.getState().categories.map((c) => [c.name, c.label])).toEqual([
    ['Mains', 'Mains'],
    ['Spirits', 'Spirits'],
  ]);
});

it('fills empty categories from the menu when the menu loads after the course list', async () => {
  vi.mocked(getMenuCourses).mockResolvedValue([]);
  await usePOSStore.getState().fetchCategories();
  vi.mocked(getRestaurantMenu).mockResolvedValueOnce([
    { item: 'A', item_name: 'Rib-Eye', rate: 1, course: 'Mains', course_label: 'Mains' },
    { item: 'B', item_name: 'Gin', rate: 1, course: 'Spirits', course_label: 'Spirits' },
  ] as never);
  usePOSStore.setState({ posProfile: { name: 'Till A', restaurant: 'R1' } as never });
  await usePOSStore.getState().fetchMenuItems();
  expect(usePOSStore.getState().categories.map((c) => c.name)).toEqual(['Mains', 'Spirits']);
});

it('keeps the server course list when the menu loads, even for courses with no items', async () => {
  const withIcons = [
    { name: 'Starters', label: 'Starters', icon: 'soup' },
    { name: 'Mains', label: 'Mains', icon: 'utensils' },
  ];
  vi.mocked(getMenuCourses).mockResolvedValue(withIcons as never);
  await usePOSStore.getState().fetchCategories();
  vi.mocked(getRestaurantMenu).mockResolvedValueOnce([
    { item: 'A', item_name: 'Rib-Eye', rate: 1, course: 'Mains', course_label: 'Mains' },
  ] as never);
  usePOSStore.setState({ posProfile: { name: 'Till A', restaurant: 'R1' } as never });
  await usePOSStore.getState().fetchMenuItems();
  expect(usePOSStore.getState().categories).toEqual(withIcons);
});

it('re-derives fallback categories when a menu switch loads different courses', async () => {
  vi.mocked(getMenuCourses).mockResolvedValue([]);
  usePOSStore.setState({ posProfile: { name: 'Till A', restaurant: 'R1' } as never });
  vi.mocked(getRestaurantMenu).mockResolvedValueOnce([
    { item: 'A', item_name: 'Rib-Eye', rate: 1, course: 'Mains', course_label: 'Mains' },
  ] as never);
  await usePOSStore.getState().fetchMenuItems();
  await usePOSStore.getState().fetchCategories();
  vi.mocked(getRestaurantMenu).mockResolvedValueOnce([
    { item: 'B', item_name: 'Gin', rate: 1, course: 'Spirits', course_label: 'Spirits' },
  ] as never);
  await usePOSStore.getState().fetchMenuItems();
  expect(usePOSStore.getState().categories.map((c) => c.name)).toEqual(['Spirits']);
});

it('clears a selected category that the refreshed list no longer has', async () => {
  sessionStorage.setItem('menuCategories', JSON.stringify([{ name: 'Old', label: 'Old' }]));
  usePOSStore.setState({ selectedCategory: 'Old' });
  vi.mocked(getMenuCourses).mockResolvedValue(courses as never);
  await usePOSStore.getState().fetchCategories();
  expect(usePOSStore.getState().selectedCategory).toBe('');
});

it('keeps a selected category that the refreshed list still has', async () => {
  usePOSStore.setState({ selectedCategory: 'Mains' });
  vi.mocked(getMenuCourses).mockResolvedValue(courses as never);
  await usePOSStore.getState().fetchCategories();
  expect(usePOSStore.getState().selectedCategory).toBe('Mains');
});

const menu = (...rows: [string, string][]) =>
  rows.map(([item, course]) => ({ item, item_name: item, rate: 1, course, course_label: course }));

it('re-derives fallback categories across two menu loads with no course fetch between', async () => {
  usePOSStore.setState({ posProfile: { name: 'Till A', restaurant: 'R1' } as never });
  vi.mocked(getRestaurantMenu).mockResolvedValueOnce(menu(['Rib-Eye', 'Mains']) as never);
  await usePOSStore.getState().fetchMenuItems();
  vi.mocked(getRestaurantMenu).mockResolvedValueOnce(menu(['Gin', 'Spirits']) as never);
  await usePOSStore.getState().fetchMenuItems();
  expect(usePOSStore.getState().categories.map((c) => c.name)).toEqual(['Spirits']);
});

it('clears a selected category that a newly loaded menu no longer has', async () => {
  usePOSStore.setState({ posProfile: { name: 'Till A', restaurant: 'R1' } as never, selectedCategory: 'Mains' });
  vi.mocked(getRestaurantMenu).mockResolvedValueOnce(menu(['Gin', 'Spirits']) as never);
  await usePOSStore.getState().fetchMenuItems();
  expect(usePOSStore.getState().selectedCategory).toBe('');
});

it('keeps a cached server list over item courses when revalidation fails', async () => {
  usePOSStore.setState({ posProfile: { name: 'Till A', restaurant: 'R1' } as never });
  vi.mocked(getRestaurantMenu).mockResolvedValueOnce(menu(['Rib-Eye', 'Mains']) as never);
  await usePOSStore.getState().fetchMenuItems();
  sessionStorage.setItem('menuCategories', JSON.stringify(courses));
  vi.mocked(getMenuCourses).mockRejectedValue(new Error('offline'));
  await usePOSStore.getState().fetchCategories();
  vi.mocked(getRestaurantMenu).mockResolvedValueOnce(menu(['Soup', 'Starters']) as never);
  await usePOSStore.getState().fetchMenuItems();
  expect(usePOSStore.getState().categories.map((c) => c.name)).toEqual(['Mains', 'Spirits']);
});
