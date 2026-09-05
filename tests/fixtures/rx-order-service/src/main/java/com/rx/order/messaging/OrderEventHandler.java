package com.rx.order.messaging;

import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.stereotype.Component;

@Component
public class OrderEventHandler {

    private final KafkaTemplate<String, String> kafkaTemplate;

    public OrderEventHandler(KafkaTemplate<String, String> kafkaTemplate) {
        this.kafkaTemplate = kafkaTemplate;
    }

    @KafkaListener(topics = "payment.completed", groupId = "rx-order")
    public void onPaymentCompleted(String message) {
        // mark order paid
        kafkaTemplate.send("order.fulfilled", message);
    }
}
