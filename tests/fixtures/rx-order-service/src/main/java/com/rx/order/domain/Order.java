package com.rx.order.domain;

import jakarta.persistence.Entity;
import jakarta.persistence.Table;
import jakarta.persistence.Id;

@Entity
@Table(name = "orders")
public class Order {

    @Id
    private Long id;
    private String status;
    private Long patientId;
    private java.math.BigDecimal totalAmount;

    public Long getId() {
        return id;
    }
}
