// The till's category rail must follow the menu it shows. A category list
// cached in sessionStorage before the menu had courses must not hide them.
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { usePOSStore } from '../src/store/pos-store';
import { getMenuCourses } from '../src/lib/menu-course-api';

vi.mock('../src/lib/menu-course-api', () => ({ getMenuCourses: vi.fn() }));
vi.mock('../src/lib/menu-api', () => ({ getRestaurantMenu: vi.fn(async () => []), getAggregatorMenu: vi.fn(async () => []) }));

const courses = [
  { name: 'Mains', label: 'Mains', icon: null },
  { name: 'Spirits', label: 'Spirits', icon: null },
];

beforeEach(() => {
  sessionStorage.clear();
  vi.mocked(getMenuCourses).mockReset();
  usePOSStore.setState({ categories: [], menuItems: [] });
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
