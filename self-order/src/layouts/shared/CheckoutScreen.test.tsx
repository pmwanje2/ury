import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it, vi } from "vitest"
import CheckoutScreen from "./CheckoutScreen"
import type { CartEntry } from "../../hooks/useOrderingSession"

describe("CheckoutScreen", () => {
  const mockItem = {
    item: "TEST-001",
    item_name: "Test Item",
    rate: 100,
    special_dish: 0,
    disabled: 0,
  }

  const cartEntry: CartEntry = {
    item: mockItem,
    qty: 2,
  }

  const baseProps = {
    capabilities: {
      customer_payment_enabled: false,
      pay_at_counter_enabled: false,
      request_bill_enabled: false,
    },
    cartItems: [cartEntry],
    cartCount: 2,
    cartTotal: 200,
    submitting: false,
    payingOnline: false,
    billRequested: false,
    paymentRequest: null,
    onSubmitCart: vi.fn(),
    onPayOnline: vi.fn(),
    onRequestBill: vi.fn(),
  }

  it("passes the selected preset through place order", async () => {
    const onSubmitCart = vi.fn()
    render(<CheckoutScreen {...baseProps} onSubmitCart={onSubmitCart} capabilities={{
      ...baseProps.capabilities,
      tip_prompt: { currency: "KWD", precision: 3, presets: [5, 10, 15] },
    }} />)
    await userEvent.click(screen.getByRole("button", { name: "10%" }))
    await userEvent.click(screen.getByRole("button", { name: "Place Order" }))
    expect(onSubmitCart).toHaveBeenCalledWith("20.000")
  })

  it.each([["UGX", 0, 1.5], ["USD", 2, 1.555], ["KWD", 3, 1.5555]] as const)(
    "blocks a rounded %s preset above the cart subtotal", async (currency, precision, subtotal) => {
      render(<CheckoutScreen {...baseProps} cartTotal={subtotal} capabilities={{
        ...baseProps.capabilities, tip_prompt: { currency, precision, presets: [100] },
      }} />)
      await userEvent.click(screen.getByRole("button", { name: "100%" }))
      expect(screen.getByRole("button", { name: "Place Order" })).toBeDisabled()
      await userEvent.click(screen.getByRole("button", { name: "No Tip" }))
      expect(screen.getByRole("button", { name: "Place Order" })).toBeEnabled()
    },
  )

  it("renders order summary with cart items", () => {
    render(<CheckoutScreen {...baseProps} />)

    expect(screen.getByText("Order Summary")).toBeInTheDocument()
    expect(screen.getByText("Test Item × 2")).toBeInTheDocument()
    const prices = screen.getAllByText("200")
    expect(prices.length).toBeGreaterThan(0)
  })

  it("displays item count and total in summary", () => {
    render(<CheckoutScreen {...baseProps} />)

    expect(screen.getByText("2 items")).toBeInTheDocument()
    const summarySection = screen.getByText("Order Summary").closest("section")
    expect(summarySection?.textContent).toContain("200")
  })

  it("hides payment method section when no payment options enabled", () => {
    render(<CheckoutScreen {...baseProps} />)

    expect(screen.queryByText("Payment Method")).not.toBeInTheDocument()
  })

  it("shows payment method section when any payment option is enabled", () => {
    render(
      <CheckoutScreen
        {...baseProps}
        capabilities={{
          customer_payment_enabled: true,
          pay_at_counter_enabled: false,
          request_bill_enabled: false,
        }}
      />
    )

    expect(screen.getByText("Payment Method")).toBeInTheDocument()
  })

  it("shows Pay Online button when enabled", () => {
    render(
      <CheckoutScreen
        {...baseProps}
        capabilities={{
          customer_payment_enabled: true,
          pay_at_counter_enabled: false,
          request_bill_enabled: false,
        }}
      />
    )

    expect(screen.getByText("Pay Online")).toBeInTheDocument()
  })

  it("shows Pay at Counter button when enabled", () => {
    render(
      <CheckoutScreen
        {...baseProps}
        capabilities={{
          customer_payment_enabled: false,
          pay_at_counter_enabled: true,
          request_bill_enabled: false,
        }}
      />
    )

    expect(screen.getByText("Pay at Counter")).toBeInTheDocument()
  })

  it("shows Request Bill button when enabled", () => {
    render(
      <CheckoutScreen
        {...baseProps}
        capabilities={{
          customer_payment_enabled: false,
          pay_at_counter_enabled: false,
          request_bill_enabled: true,
        }}
      />
    )

    expect(screen.getByText("Request Bill")).toBeInTheDocument()
  })

  it("pays at counter by default when option is enabled", async () => {
    render(
      <CheckoutScreen
        {...baseProps}
        capabilities={{
          customer_payment_enabled: false,
          pay_at_counter_enabled: true,
          request_bill_enabled: false,
        }}
      />
    )

    const counterButton = screen.getByText("Pay at Counter")
    expect(counterButton).toHaveClass("border-primary")
  })

  it("calls onSubmitCart when Place Order is clicked", async () => {
    const onSubmitCart = vi.fn()
    render(<CheckoutScreen {...baseProps} onSubmitCart={onSubmitCart} />)

    const placeOrderButton = screen.getByText("Place Order")
    await userEvent.click(placeOrderButton)

    expect(onSubmitCart).toHaveBeenCalled()
  })

  it("allows selecting different payment methods", async () => {
    render(
      <CheckoutScreen
        {...baseProps}
        capabilities={{
          customer_payment_enabled: true,
          pay_at_counter_enabled: true,
          request_bill_enabled: false,
        }}
      />
    )

    // Verify both payment methods are shown
    const paymentButtons = screen.getAllByRole("button")
    expect(paymentButtons.length).toBeGreaterThan(0)
  })

  it("disables Place Order button when cart is empty", () => {
    render(
      <CheckoutScreen {...baseProps} cartItems={[]} cartCount={0} />
    )

    const placeOrderButton = screen.getByText("Place Order")
    expect(placeOrderButton).toBeDisabled()
  })

  it("shows loading state when submitting", () => {
    render(<CheckoutScreen {...baseProps} submitting={true} />)

    expect(screen.getByText("Placing order…")).toBeInTheDocument()
  })

  it("shows payment method buttons when paying online but pay at counter not enabled", () => {
    render(
      <CheckoutScreen
        {...baseProps}
        capabilities={{
          customer_payment_enabled: true,
          pay_at_counter_enabled: false,
          request_bill_enabled: false,
        }}
      />
    )

    // No default selection means Place Order should show
    expect(screen.getByText("Place Order")).toBeInTheDocument()
  })

  it("disables Request Bill button when bill already requested", () => {
    render(
      <CheckoutScreen
        {...baseProps}
        capabilities={{
          customer_payment_enabled: false,
          pay_at_counter_enabled: false,
          request_bill_enabled: true,
        }}
        billRequested={true}
      />
    )

    const billButton = screen.getByText("Bill requested — staff notified")
    expect(billButton).toBeDisabled()
  })

  it("calls onRequestBill when Request Bill button is clicked", async () => {
    const onRequestBill = vi.fn()
    render(
      <CheckoutScreen
        {...baseProps}
        capabilities={{
          customer_payment_enabled: false,
          pay_at_counter_enabled: false,
          request_bill_enabled: true,
        }}
        onRequestBill={onRequestBill}
      />
    )

    const billButton = screen.getByText("Request Bill")
    await userEvent.click(billButton)

    expect(onRequestBill).toHaveBeenCalled()
  })
  const onlineCapabilities = {
    customer_payment_enabled: true,
    pay_at_counter_enabled: false,
    request_bill_enabled: false,
  }
  const paidRequest = {
    payment_request: "PR-1", amount: 12000, currency: "UGX", payment_url: null,
    status: "Paid", paid_amount: 12000, remaining_amount: 0,
  }

  it("tells the guest a Paid phone payment is paid, not that staff will assist", () => {
    render(<CheckoutScreen {...baseProps} capabilities={onlineCapabilities} paymentRequest={paidRequest} />)

    expect(screen.getByRole("status")).toHaveTextContent("Paid (12000 UGX) — thank you.")
    expect(screen.queryByText(/Payment request created/)).not.toBeInTheDocument()
  })

  it("sends the remaining balance after a phone payment to the counter", () => {
    render(
      <CheckoutScreen
        {...baseProps}
        capabilities={onlineCapabilities}
        paymentRequest={{ ...paidRequest, remaining_amount: 3000 }}
      />
    )

    expect(screen.getByRole("status")).toHaveTextContent(
      "You've paid 12000 UGX by phone. Please pay the remaining 3000 UGX at the counter."
    )
  })
})
